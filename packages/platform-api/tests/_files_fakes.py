"""The files explorer's test doubles and fixtures: an in-memory S3, an audit store, and an app wired to both."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.audit import AuditLog
from openlakeforge_platform_api.settings import Settings

if TYPE_CHECKING:
    from conftest import Mint

WHEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
BUCKETS = {"dev": ["olf-dev-bronze", "olf-dev-silver"], "uat": ["olf-uat-bronze"], "prod": ["olf-prod-bronze"]}
OPS = "olf-ops"
DEV = "olf-dev-bronze"
OCTET = {"Content-Type": "application/octet-stream"}


class Body:
    """A streaming body that, like botocore's, is read in bounded pieces; an unbounded read fails the test."""

    def __init__(self, data: bytes) -> None:
        self._io = io.BytesIO(data)
        self.closed = False
        self.largest_read = 0

    def read(self, amt: int | None = None) -> bytes:
        assert amt is not None and amt > 0, "the whole object must not be read into memory at once"
        self.largest_read = max(self.largest_read, amt)
        return self._io.read(amt)

    def iter_chunks(self, size: int) -> Iterator[bytes]:
        while chunk := self.read(size):
            yield chunk

    def close(self) -> None:
        self.closed = True


class FakeS3:
    """list_buckets, list_objects_v2 (delimiter and continuation tokens) and get_object over a dict."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str | None]] = {}
        self.fail: Callable[[], Exception] | None = None
        self.deny_list_buckets = False
        self.bodies: list[Body] = []
        # The write routes: every call by name, an error to raise per method, and multipart state.
        self.calls: list[str] = []
        self.fail_ops: dict[str, Callable[[], Exception]] = {}
        # Code to run when a call starts, to let another writer in between a route's check and its write.
        self.hooks: dict[str, Callable[[], None]] = {}
        self.lock = threading.Lock()
        self.copies_allowed: int | None = None
        self.uploads: dict[str, list[bytes]] = {}
        self.part_sizes: list[int] = []
        self.aborted: list[str] = []
        self.delete_errors: dict[str, str] = {}

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.hooks:
            self.hooks[name]()
        if name in self.fail_ops:
            raise self.fail_ops[name]()

    def _create(self, bucket: str, key: str, data: bytes, if_none_match: str | None) -> None:
        """Store `data`; with `If-None-Match: *` the check and the write are one atomic step, as S3 makes them."""
        with self.lock:
            if if_none_match == "*" and (bucket, key) in self.objects:
                raise _client_error("PreconditionFailed")
            self.objects[(bucket, key)] = (data, None)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._call("head_object")
        if (Bucket, Key) not in self.objects:
            raise _client_error("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)][0]), "ETag": self.etag(Bucket, Key)}

    def put_object(  # noqa: N803
        self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None
    ) -> dict[str, Any]:
        self._call("put_object")
        self._create(Bucket, Key, Body, IfNoneMatch)
        return {}

    def create_multipart_upload(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._call("create_multipart_upload")
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads[upload_id] = []
        return {"UploadId": upload_id}

    def upload_part(  # noqa: N803
        self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes
    ) -> dict[str, Any]:
        self._call("upload_part")
        assert PartNumber == len(self.uploads[UploadId]) + 1
        self.uploads[UploadId].append(Body)
        self.part_sizes.append(len(Body))
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(  # noqa: N803
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, Any],
        IfNoneMatch: str | None = None,
    ) -> dict[str, Any]:
        self._call("complete_multipart_upload")
        assert [part["PartNumber"] for part in MultipartUpload["Parts"]] == list(
            range(1, len(self.uploads[UploadId]) + 1)
        )
        self._create(Bucket, Key, b"".join(self.uploads[UploadId]), IfNoneMatch)
        del self.uploads[UploadId]
        return {}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, Any]:  # noqa: N803
        self._call("abort_multipart_upload")
        self.uploads.pop(UploadId, None)
        self.aborted.append(UploadId)
        return {}

    def copy_object(  # noqa: N803
        self,
        *,
        Bucket: str,
        Key: str,
        CopySource: dict[str, str],
        IfNoneMatch: str | None = None,
        CopySourceIfMatch: str | None = None,
    ) -> dict[str, Any]:
        self._call("copy_object")
        if self.copies_allowed is not None:
            if self.copies_allowed <= 0:
                raise _client_error("InternalError")
            self.copies_allowed -= 1
        source = (CopySource["Bucket"], CopySource["Key"])
        if CopySourceIfMatch is not None and self.etag(*source) != CopySourceIfMatch:
            raise _client_error("PreconditionFailed")
        self._create(Bucket, Key, self.objects[source][0], IfNoneMatch)
        return {"CopyObjectResult": {"ETag": self.etag(Bucket, Key)}}

    def delete_object(self, *, Bucket: str, Key: str, IfMatch: str | None = None) -> dict[str, Any]:  # noqa: N803
        self._call("delete_object")
        if IfMatch is not None and ((Bucket, Key) not in self.objects or self.etag(Bucket, Key) != IfMatch):
            raise _client_error("PreconditionFailed")
        self.objects.pop((Bucket, Key), None)
        return {}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self._call("delete_objects")
        assert len(Delete["Objects"]) <= 1000
        errors = []
        for entry in Delete["Objects"]:
            key = entry["Key"]
            if key in self.delete_errors:
                errors.append({"Key": key, "Code": self.delete_errors[key], "Message": "secret"})
            elif "ETag" in entry and ((Bucket, key) not in self.objects or self.etag(Bucket, key) != entry["ETag"]):
                errors.append({"Key": key, "Code": "PreconditionFailed", "Message": "secret"})
            else:
                self.objects.pop((Bucket, key), None)
        return {"Errors": errors}

    def etag(self, bucket: str, key: str) -> str:
        return '"' + hashlib.md5(self.objects[(bucket, key)][0], usedforsecurity=False).hexdigest() + '"'

    def keys(self, bucket: str = "olf-dev-bronze") -> list[str]:
        return sorted(key for name, key in self.objects if name == bucket)

    def put(self, bucket: str, key: str, data: bytes = b"", content_type: str | None = None) -> None:
        self.objects[(bucket, key)] = (data, content_type)

    def list_buckets(self) -> dict[str, Any]:
        if self.deny_list_buckets:
            raise _client_error("AccessDenied")
        return {"Buckets": [{"Name": name, "CreationDate": WHEN} for name in ("olf-dev-bronze", OPS)]}

    def list_objects_v2(  # noqa: N803 - boto3's names
        self, *, Bucket: str, Prefix: str = "", Delimiter: str = "", MaxKeys: int = 1000, ContinuationToken: str = ""
    ) -> dict[str, Any]:
        if self.fail:
            raise self.fail()
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        entries: list[tuple[str, str]] = []  # ("p", prefix) or ("k", key), in listing order
        for key in keys:
            rest = key[len(Prefix) :]
            if Delimiter and Delimiter in rest:
                entry = ("p", Prefix + rest.split(Delimiter)[0] + Delimiter)
                if entry not in entries:
                    entries.append(entry)
            else:
                entries.append(("k", key))
        start = int(ContinuationToken or 0)
        page = entries[start : start + MaxKeys]
        truncated = start + MaxKeys < len(entries)
        result: dict[str, Any] = {
            "CommonPrefixes": [{"Prefix": value} for kind, value in page if kind == "p"],
            "Contents": [
                {
                    "Key": value,
                    "Size": len(self.objects[(Bucket, value)][0]),
                    "LastModified": WHEN,
                    "ETag": self.etag(Bucket, value),
                }
                for kind, value in page
                if kind == "k"
            ],
            "IsTruncated": truncated,
        }
        if truncated:
            result["NextContinuationToken"] = str(start + MaxKeys)
        return result

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if self.fail:
            raise self.fail()
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey")
        data, content_type = self.objects[(Bucket, Key)]
        body = Body(data)
        self.bodies.append(body)
        result: dict[str, Any] = {"Body": body, "ContentLength": len(data)}
        if content_type:
            result["ContentType"] = content_type
        return result


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


@pytest.fixture
def s3() -> FakeS3:
    fake = FakeS3()
    fake.put("olf-dev-bronze", "orders/2026/a.csv", b"id\n1\n", "text/csv")
    fake.put("olf-dev-bronze", "orders/2026/b.csv", b"id\n2\n")
    fake.put("olf-dev-bronze", "orders/", b"")
    fake.put("olf-dev-bronze", "readme.txt", b"hello")
    fake.put("olf-dev-bronze", 'café "1".txt', b"x")
    fake.put("olf-uat-bronze", "u.txt", b"u")
    fake.put("olf-prod-bronze", "p.txt", b"p")
    fake.put(OPS, "activations/dev/logs/x.gz", b"log")
    fake.put(OPS, "audit/date=2026-11-01/r.json", b"{}")
    return fake


def _settings(settings: Settings, **extra: Any) -> Settings:
    raw = {
        "issuer_url": settings.issuer_url,
        "audiences": list(settings.audiences),
        "role_claim": settings.role_claim,
        "role_mapping": {role: list(values) for role, values in settings.role_mapping.items()},
        "precedence": list(settings.precedence),
        "stages": ["dev", "uat", "prod"],
        "file_buckets": BUCKETS,
        "ops_bucket": OPS,
    }
    return Settings.from_mapping({**raw, **extra})


class AuditStore:
    """put_object into a dict, as the audit writer's boto3 client answers it; `down` refuses every write."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.down = False
        # A test that sets `hold` parks the first write on it, so a request can be seen to wait on the store
        # while others are served; `parked` is set once a write is waiting, `stalled` if the wait ran out.
        self.hold: threading.Event | None = None
        self.parked = threading.Event()
        self.stalled = False

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict[str, Any]:  # noqa: N803
        if self.down:
            raise ConnectionError("seaweedfs refused the connection")
        if self.hold is not None and not self.parked.is_set():
            self.parked.set()
            self.stalled = not self.hold.wait(timeout=10)
        self.records[Key] = json.loads(Body)
        return {}

    def phases(self) -> list[str]:
        return [record["phase"] for _key, record in sorted(self.records.items())]


@pytest.fixture
def audit_store() -> AuditStore:
    return AuditStore()


@pytest.fixture
def api(settings: Settings, s3: FakeS3, audit_store: AuditStore) -> TestClient:
    configured = _settings(settings, audit_base_uri=f"s3://{OPS}/audit/")
    audit = AuditLog(audit_store, configured.audit_base_uri, emit=lambda line: None)
    return TestClient(create_app(configured, files_store=s3, audit=audit))


def _as(mint: Mint, role: str) -> dict[str, str]:
    # Imported here: conftest imports this module for its fixtures.
    from conftest import ROLE_MAPPING, bearer

    return bearer(mint(groups=list(ROLE_MAPPING[role])))


def _base(stage: str = "dev", bucket: str = "olf-dev-bronze") -> str:
    return f"/api/v1/stages/{stage}/buckets/{bucket}/objects"


def _client(settings: Settings, s3: FakeS3, audit_store: AuditStore, **extra: Any) -> TestClient:
    configured = _settings(settings, **{"audit_base_uri": f"s3://{OPS}/audit/", **extra})
    audit = AuditLog(audit_store, configured.audit_base_uri, emit=lambda line: None)
    return TestClient(create_app(configured, files_store=s3, audit=audit))
