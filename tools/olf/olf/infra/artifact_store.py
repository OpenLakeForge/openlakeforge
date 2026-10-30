"""Transport-neutral content-addressed artifact storage.

Bucket and client resolution for the project revision (`olf project build`)
and stage activations, through the in-cluster/cloud S3 access path.
`RevisionStore` additionally lets the project revision publish to a local
directory (`olf project build --output DIR`) without a cluster or cloud
credentials.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from olf.common import config


class ArtifactStoreError(RuntimeError):
    """An artifact store cannot be reached, resolved, or written to safely."""


def artifact_bucket() -> str:
    bucket = config.env("OPENLAKEFORGE_OPS_BUCKET_NAME") or config.env("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME")
    if not bucket:
        raise ArtifactStoreError("no ops/artifact bucket resolved from the contract environment.")
    return bucket


@contextmanager
def artifact_storage_client(via: str, bucket: str) -> Iterator[Any]:
    from olf.infra import k8s, s3

    if via == "direct":
        from olf.infra.auth import aws_session

        region = config.env("OPENLAKEFORGE_STORAGE_REGION") or None
        yield aws_session(os.environ, region=region).client("s3", region_name=region)
        return
    if via != "port-forward":
        raise ArtifactStoreError(f"unknown --via mode: {via!r} (expected 'port-forward' or 'direct')")

    # This is the shared admin storage identity (OPENLAKEFORGE_OPS_STORAGE_*),
    # not a stage's own OPENLAKEFORGE_STORAGE_* one: immutable project
    # revisions (olf.infra.project_revision.REVISION_PREFIX) are published
    # outside any stage's activations/<stage> prefix, so a stage's own
    # narrower identity cannot reach it, and it lives only in the shared
    # namespace, not config.namespace()'s stage-selected one.
    namespace = config.shared_namespace()
    secret_name = config.env("OPENLAKEFORGE_OPS_STORAGE_CREDENTIALS_SECRET_NAME")
    service = config.env("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAME", "seaweedfs-s3")
    remote_port = int(config.env("OPENLAKEFORGE_STORAGE_S3_SERVICE_PORT", "8333"))
    service_namespace = config.env("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAMESPACE") or namespace
    with s3.port_forward_client(
        bucket,
        service=service,
        remote_port=remote_port,
        namespace=service_namespace,
        access_key_id=k8s.secret_value(
            secret_name, config.env("OPENLAKEFORGE_OPS_STORAGE_ACCESS_KEY_ID_KEY", "AWS_ACCESS_KEY_ID"), namespace
        ),
        secret_access_key=k8s.secret_value(
            secret_name,
            config.env("OPENLAKEFORGE_OPS_STORAGE_SECRET_ACCESS_KEY_KEY", "AWS_SECRET_ACCESS_KEY"),
            namespace,
        ),
        region=config.env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1"),
    ) as client:
        yield client


class RevisionStore(Protocol):
    """Read/write immutable content-addressed objects by key."""

    def read(self, key: str) -> bytes | None: ...

    def write(self, key: str, content: bytes, *, content_type: str) -> None: ...


class S3RevisionStore:
    """Publishes through an S3-compatible client (in-cluster port-forward or direct cloud S3)."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def read(self, key: str) -> bytes | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = str((exc.response.get("Error") or {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ArtifactStoreError(f"failed to read s3://{self._bucket}/{key}") from exc
        except BotoCoreError as exc:
            raise ArtifactStoreError(f"failed to read s3://{self._bucket}/{key}") from exc
        content = response["Body"].read()
        return content if isinstance(content, bytes) else content.encode("utf-8")

    def write(self, key: str, content: bytes, *, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=content, ContentType=content_type)
        except (BotoCoreError, ClientError) as exc:
            raise ArtifactStoreError(f"failed to publish s3://{self._bucket}/{key}") from exc


class FilesystemRevisionStore:
    """Publishes into a local directory tree, for offline builds and tests."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def read(self, key: str) -> bytes | None:
        path = self._root / key
        return path.read_bytes() if path.is_file() else None

    def write(self, key: str, content: bytes, *, content_type: str) -> None:  # noqa: ARG002
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def publish_immutable(store: RevisionStore, key: str, content: bytes, *, content_type: str) -> None:
    """Publish a content-addressed object without overwriting a different one.

    A failed or partial publish leaves at most an unreferenced key; it never
    changes the content behind an already-published one.
    """
    existing = store.read(key)
    if existing is None:
        store.write(key, content, content_type=content_type)
        return
    if existing != content:
        raise ArtifactStoreError(f"immutable artifact collision at {key!r}; existing content differs.")


def read_required(store: RevisionStore, key: str) -> bytes:
    content = store.read(key)
    if content is None:
        raise ArtifactStoreError(f"missing immutable artifact {key!r}.")
    return content
