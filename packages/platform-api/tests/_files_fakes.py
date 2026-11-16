"""The files explorer's test doubles and fixtures: an in-memory S3, an audit store, and an app wired to both."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings

if TYPE_CHECKING:
    from conftest import Mint

WHEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
BUCKETS = {"dev": ["olf-dev-bronze", "olf-dev-silver"], "uat": ["olf-uat-bronze"], "prod": ["olf-prod-bronze"]}
OPS = "olf-ops"


class Body:
    def __init__(self, data: bytes) -> None:
        self._io = io.BytesIO(data)
        self.closed = False
        self.chunk_sizes: list[int] = []

    def iter_chunks(self, size: int) -> Iterator[bytes]:
        self.chunk_sizes.append(size)
        while chunk := self._io.read(size):
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
                {"Key": value, "Size": len(self.objects[(Bucket, value)][0]), "LastModified": WHEN}
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


@pytest.fixture
def api(settings: Settings, s3: FakeS3) -> TestClient:
    configured = _settings(settings, audit_base_uri=f"s3://{OPS}/audit/")
    return TestClient(create_app(configured, files_store=s3))


def _as(mint: Mint, role: str) -> dict[str, str]:
    # Imported here: conftest imports this module for its fixtures.
    from conftest import ROLE_MAPPING, bearer

    return bearer(mint(groups=list(ROLE_MAPPING[role])))


def _base(stage: str = "dev", bucket: str = "olf-dev-bronze") -> str:
    return f"/api/v1/stages/{stage}/buckets/{bucket}/objects"
