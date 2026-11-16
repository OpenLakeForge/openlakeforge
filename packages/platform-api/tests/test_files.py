"""The files explorer's read routes against an in-memory S3, with the role matrix, bucket and key checks."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from conftest import ROLE_MAPPING, Mint, bearer
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.audit import AuditLog
from openlakeforge_platform_api.files import FILE_OPERATIONS, touches_audit_prefix
from openlakeforge_platform_api.settings import Settings, SettingsError

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
        # The write routes: every call by name, an error to raise per method, and multipart state.
        self.calls: list[str] = []
        self.fail_ops: dict[str, Callable[[], Exception]] = {}
        self.copies_allowed: int | None = None
        self.uploads: dict[str, list[bytes]] = {}
        self.part_sizes: list[int] = []
        self.aborted: list[str] = []
        self.delete_errors: dict[str, str] = {}

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_ops:
            raise self.fail_ops[name]()

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._call("head_object")
        if (Bucket, Key) not in self.objects:
            raise _client_error("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)][0])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:  # noqa: N803
        self._call("put_object")
        self.objects[(Bucket, Key)] = (Body, None)
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
        self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict[str, Any]
    ) -> dict[str, Any]:
        self._call("complete_multipart_upload")
        assert [part["PartNumber"] for part in MultipartUpload["Parts"]] == list(
            range(1, len(self.uploads[UploadId]) + 1)
        )
        self.objects[(Bucket, Key)] = (b"".join(self.uploads.pop(UploadId)), None)
        return {}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, Any]:  # noqa: N803
        self._call("abort_multipart_upload")
        self.uploads.pop(UploadId, None)
        self.aborted.append(UploadId)
        return {}

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict[str, str]) -> dict[str, Any]:  # noqa: N803
        self._call("copy_object")
        if self.copies_allowed is not None:
            if self.copies_allowed <= 0:
                raise _client_error("InternalError")
            self.copies_allowed -= 1
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._call("delete_object")
        self.objects.pop((Bucket, Key), None)
        return {}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self._call("delete_objects")
        assert len(Delete["Objects"]) <= 1000
        errors = []
        for entry in Delete["Objects"]:
            if entry["Key"] in self.delete_errors:
                errors.append({"Key": entry["Key"], "Code": self.delete_errors[entry["Key"]], "Message": "secret"})
            else:
                self.objects.pop((Bucket, entry["Key"]), None)
        return {"Errors": errors}

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


class AuditStore:
    """put_object into a dict, as the audit writer's boto3 client answers it; `down` refuses every write."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.down = False

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict[str, Any]:  # noqa: N803
        if self.down:
            raise ConnectionError("seaweedfs refused the connection")
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
    return bearer(mint(groups=list(ROLE_MAPPING[role])))


def _base(stage: str = "dev", bucket: str = "olf-dev-bronze") -> str:
    return f"/api/v1/stages/{stage}/buckets/{bucket}/objects"


# settings


def test_settings_parse_the_files_fields(settings: Settings) -> None:
    parsed = _settings(settings, files_max_upload_bytes=5)

    assert parsed.file_buckets == {
        "dev": ("olf-dev-bronze", "olf-dev-silver"),
        "uat": ("olf-uat-bronze",),
        "prod": ("olf-prod-bronze",),
    }
    assert parsed.ops_bucket == OPS
    assert parsed.files_max_upload_bytes == 5


def test_settings_default_to_no_files_and_a_gibibyte_cap(settings: Settings) -> None:
    assert settings.file_buckets == {}
    assert settings.ops_bucket is None
    assert settings.files_max_upload_bytes == 1_073_741_824


def test_file_buckets_for_a_stage_that_is_not_enabled_are_refused(settings: Settings) -> None:
    with pytest.raises(SettingsError, match="file_buckets names stages that are not enabled: qa"):
        _settings(settings, file_buckets={"qa": ["b"]})


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_upload_cap_is_refused(settings: Settings, value: int) -> None:
    with pytest.raises(SettingsError, match="files_max_upload_bytes must be positive"):
        _settings(settings, files_max_upload_bytes=value)


# the role matrix


@pytest.mark.parametrize("stage", ["dev", "uat", "prod"])
def test_platform_admin_lists_objects_on_every_stage(api: TestClient, mint: Mint, stage: str) -> None:
    bucket = BUCKETS[stage][0]

    assert api.get(_base(stage, bucket), headers=_as(mint, "platform-admin")).status_code == 200


def test_data_engineer_is_limited_to_dev(api: TestClient, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")

    assert api.get(_base(), headers=headers).status_code == 200
    refused = api.get(_base("uat", "olf-uat-bronze"), headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "role 'data-engineer' may perform 'list_objects' only on stages: dev"
    assert api.get("/api/v1/stages/prod/buckets", headers=headers).status_code == 403


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_analyst_and_viewer_reach_no_files_route(api: TestClient, mint: Mint, role: str) -> None:
    headers = _as(mint, role)

    assert api.get("/api/v1/stages/dev/buckets", headers=headers).status_code == 403
    assert api.get(_base(), headers=headers).status_code == 403
    assert api.get(_base() + "/content?key=readme.txt", headers=headers).status_code == 403


def test_file_capabilities_per_role(api: TestClient, mint: Mint) -> None:
    read = list(FILE_OPERATIONS)

    admin = api.get("/api/v1/file-capabilities", headers=_as(mint, "platform-admin")).json()
    engineer = api.get("/api/v1/file-capabilities", headers=_as(mint, "data-engineer")).json()

    assert admin == {
        "stages": [{"stage": stage, "operations": read} for stage in ("dev", "uat", "prod")],
        "max_upload_bytes": 1_073_741_824,
    }
    assert engineer["stages"] == [{"stage": "dev", "operations": read}]
    for role in ("analyst", "viewer"):
        body = api.get("/api/v1/file-capabilities", headers=_as(mint, role)).json()
        assert body == {"stages": [], "max_upload_bytes": 1_073_741_824}


def test_file_capabilities_omit_a_stage_with_no_buckets(settings: Settings, s3: FakeS3, mint: Mint) -> None:
    configured = _settings(settings, file_buckets={"dev": ["olf-dev-bronze"]})
    api = TestClient(create_app(configured, files_store=s3))

    body = api.get("/api/v1/file-capabilities", headers=_as(mint, "platform-admin")).json()

    assert [entry["stage"] for entry in body["stages"]] == ["dev"]


# buckets


def test_buckets_come_in_configured_order_with_creation_times(api: TestClient, mint: Mint) -> None:
    body = api.get("/api/v1/stages/dev/buckets", headers=_as(mint, "data-engineer")).json()

    assert body == {
        "buckets": [
            {"name": "olf-dev-bronze", "created_at": WHEN.isoformat()},
            {"name": "olf-dev-silver", "created_at": None},
        ]
    }


def test_only_platform_admin_sees_the_ops_bucket(api: TestClient, mint: Mint) -> None:
    admin = api.get("/api/v1/stages/dev/buckets", headers=_as(mint, "platform-admin")).json()
    engineer = api.get("/api/v1/stages/dev/buckets", headers=_as(mint, "data-engineer")).json()

    assert [bucket["name"] for bucket in admin["buckets"]] == ["olf-dev-bronze", "olf-dev-silver", OPS]
    assert OPS not in [bucket["name"] for bucket in engineer["buckets"]]


def test_listing_buckets_survives_a_denied_account_listing(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    s3.deny_list_buckets = True

    response = api.get("/api/v1/stages/dev/buckets", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert [bucket["created_at"] for bucket in response.json()["buckets"]] == [None, None]


def test_the_ops_bucket_is_readable_by_platform_admin_only(api: TestClient, mint: Mint) -> None:
    ops = _base("dev", OPS)

    assert api.get(ops + "?prefix=activations/", headers=_as(mint, "platform-admin")).status_code == 200
    refused = api.get(ops, headers=_as(mint, "data-engineer"))
    assert refused.status_code == 404
    assert refused.json()["detail"] == f"bucket '{OPS}' is not in stage 'dev'"


def test_a_bucket_of_another_stage_is_not_found(api: TestClient, mint: Mint) -> None:
    response = api.get(_base("dev", "olf-uat-bronze"), headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert response.json()["detail"] == "bucket 'olf-uat-bronze' is not in stage 'dev'"


# listing


def test_listing_the_root_shows_folders_and_files(api: TestClient, mint: Mint) -> None:
    body = api.get(_base(), headers=_as(mint, "data-engineer")).json()

    assert body["prefix"] == ""
    assert body["folders"] == [{"prefix": "orders/"}]
    assert [file["key"] for file in body["files"]] == ['café "1".txt', "readme.txt"]
    assert body["files"][1] == {"key": "readme.txt", "size": 5, "last_modified": WHEN.isoformat()}
    assert body["next_cursor"] is None


def test_the_folder_marker_is_not_a_file(api: TestClient, mint: Mint) -> None:
    body = api.get(_base() + "?prefix=orders/", headers=_as(mint, "data-engineer")).json()

    assert body["folders"] == [{"prefix": "orders/2026/"}]
    assert body["files"] == []
    inner = api.get(_base() + "?prefix=orders/2026/", headers=_as(mint, "data-engineer")).json()
    assert [file["key"] for file in inner["files"]] == ["orders/2026/a.csv", "orders/2026/b.csv"]


def test_pages_chain_through_next_cursor(api: TestClient, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")
    seen: list[str] = []
    cursor = ""
    while True:
        body = api.get(_base() + f"?prefix=orders/2026/&limit=1&cursor={cursor}", headers=headers).json()
        seen += [file["key"] for file in body["files"]]
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]

    assert seen == ["orders/2026/a.csv", "orders/2026/b.csv"]


@pytest.mark.parametrize("limit", [0, 1001, -5, "x"])
def test_a_limit_outside_1_to_1000_is_refused(api: TestClient, mint: Mint, limit: object) -> None:
    assert api.get(_base() + f"?limit={limit}", headers=_as(mint, "data-engineer")).status_code == 422


def test_the_limit_bounds_are_accepted(api: TestClient, mint: Mint) -> None:
    for limit in (1, 1000):
        assert api.get(_base() + f"?limit={limit}", headers=_as(mint, "data-engineer")).status_code == 200


@pytest.mark.parametrize("prefix", ["orders", "/orders/", "a/../b/", "a/../", "bad\x01/"])
def test_an_invalid_prefix_is_refused(api: TestClient, mint: Mint, prefix: str) -> None:
    response = api.get(_base(), params={"prefix": prefix}, headers=_as(mint, "data-engineer"))

    assert response.status_code == 422


def test_listing_passes_the_delimiter_limit_and_cursor_to_s3(settings: Settings, s3: FakeS3, mint: Mint) -> None:
    calls: list[dict[str, Any]] = []
    original = s3.list_objects_v2

    def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return original(**kwargs)

    s3.list_objects_v2 = spy  # type: ignore[method-assign]
    api = TestClient(create_app(_settings(settings), files_store=s3))

    api.get(_base() + "?prefix=orders/&limit=7&cursor=0", headers=_as(mint, "data-engineer"))

    assert calls == [
        {"Bucket": "olf-dev-bronze", "Prefix": "orders/", "Delimiter": "/", "MaxKeys": 7, "ContinuationToken": "0"}
    ]


# download


def test_download_streams_the_body_with_its_headers(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    response = api.get(_base() + "/content?key=orders/2026/a.csv", headers=_as(mint, "data-engineer"))

    assert response.status_code == 200
    assert response.content == b"id\n1\n"
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-length"] == "5"
    assert response.headers["content-disposition"] == "attachment; filename=\"a.csv\"; filename*=UTF-8''a.csv"
    assert s3.bodies[0].chunk_sizes == [64 * 1024]
    assert s3.bodies[0].closed


def test_download_defaults_to_octet_stream_and_encodes_the_filename(api: TestClient, mint: Mint) -> None:
    response = api.get(_base() + "/content", params={"key": 'café "1".txt'}, headers=_as(mint, "data-engineer"))

    assert response.headers["content-type"] == "application/octet-stream"
    assert (
        response.headers["content-disposition"]
        == "attachment; filename=\"caf_ _1_.txt\"; filename*=UTF-8''caf%C3%A9%20%221%22.txt"
    )


def test_download_of_a_missing_key_is_not_found(api: TestClient, mint: Mint) -> None:
    response = api.get(_base() + "/content?key=nope.txt", headers=_as(mint, "data-engineer"))

    assert response.status_code == 404


def test_the_ops_bucket_audit_prefix_can_be_read_by_platform_admin(api: TestClient, mint: Mint) -> None:
    key = "audit/date=2026-11-01/r.json"

    response = api.get(_base("dev", OPS) + "/content", params={"key": key}, headers=_as(mint, "platform-admin"))

    assert response.status_code == 200


# keys


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "..", "a/..", "a\x00b", "a\nb", "k" * 1025, "é" * 513])
def test_an_invalid_key_is_refused(api: TestClient, mint: Mint, key: str) -> None:
    response = api.get(_base() + "/content", params={"key": key}, headers=_as(mint, "data-engineer"))

    assert response.status_code == 422


def test_a_key_of_exactly_1024_bytes_and_dots_inside_a_name_are_allowed(
    api: TestClient, s3: FakeS3, mint: Mint
) -> None:
    long_key = "k" * 1024
    s3.put("olf-dev-bronze", long_key, b"x")
    s3.put("olf-dev-bronze", "a..b/c...d", b"y")
    headers = _as(mint, "data-engineer")

    assert api.get(_base() + "/content", params={"key": long_key}, headers=headers).status_code == 200
    assert api.get(_base() + "/content", params={"key": "a..b/c...d"}, headers=headers).status_code == 200


def test_a_missing_key_parameter_is_refused(api: TestClient, mint: Mint) -> None:
    assert api.get(_base() + "/content", headers=_as(mint, "data-engineer")).status_code == 422


# the store


def test_a_missing_store_answers_503(settings: Settings, mint: Mint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENLAKEFORGE_FILES_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("OPENLAKEFORGE_FILES_SECRET_ACCESS_KEY", raising=False)
    api = TestClient(create_app(_settings(settings)))
    headers = _as(mint, "platform-admin")

    for path in ("/api/v1/stages/dev/buckets", _base(), _base() + "/content?key=a"):
        response = api.get(path, headers=headers)
        assert response.status_code == 503
        assert response.json()["detail"] == "the files store is not configured"


def test_the_store_is_built_only_from_the_files_key(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "log-reader")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "log-secret")
    monkeypatch.delenv("OPENLAKEFORGE_FILES_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("OPENLAKEFORGE_FILES_SECRET_ACCESS_KEY", raising=False)

    assert create_app(_settings(settings)).state.files_store is None

    monkeypatch.setenv("OPENLAKEFORGE_FILES_ACCESS_KEY_ID", "files")
    monkeypatch.setenv("OPENLAKEFORGE_FILES_SECRET_ACCESS_KEY", "files-secret")

    assert create_app(_settings(settings)).state.files_store is not None
    assert create_app(settings).state.files_store is None  # no file_buckets configured


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (lambda: _client_error("AccessDenied"), 502),
        (lambda: _client_error("NoSuchBucket"), 404),
        (lambda: _client_error("InternalError"), 502),
        (lambda: EndpointConnectionError(endpoint_url="http://s3.invalid"), 503),
    ],
)
def test_botocore_errors_map_to_a_status(
    api: TestClient, s3: FakeS3, mint: Mint, error: Callable[[], Exception], status: int
) -> None:
    s3.fail = error
    headers = _as(mint, "data-engineer")

    assert api.get(_base(), headers=headers).status_code == status
    assert api.get(_base() + "/content?key=readme.txt", headers=headers).status_code == status


def test_an_access_denied_detail_does_not_leak_the_endpoint(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    s3.fail = lambda: _client_error("AccessDenied")

    detail = api.get(_base(), headers=_as(mint, "data-engineer")).json()["detail"]

    assert detail == "the object store denied the platform API's files key"


def test_listing_buckets_with_the_store_unreachable_answers_503(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    def unreachable() -> dict[str, Any]:
        raise EndpointConnectionError(endpoint_url="http://s3.invalid")

    s3.list_buckets = unreachable  # type: ignore[method-assign]

    assert api.get("/api/v1/stages/dev/buckets", headers=_as(mint, "platform-admin")).status_code == 503


# the audit prefix helper the write routes use


@pytest.mark.parametrize(
    ("bucket", "key", "touches"),
    [
        (OPS, "audit/date=2026-11-01/r.json", True),
        (OPS, "audit/", True),
        (OPS, "audit/x", True),
        (OPS, "", False),
        (OPS, "activations/dev/x", False),
        (OPS, "auditing/x", False),
        ("olf-dev-bronze", "audit/x", False),
    ],
)
def test_touches_audit_prefix(settings: Settings, bucket: str, key: str, touches: bool) -> None:
    configured = _settings(settings, audit_base_uri=f"s3://{OPS}/audit/")

    assert touches_audit_prefix(configured, bucket, key) is touches


def test_a_deployment_without_audit_has_no_protected_prefix(settings: Settings) -> None:
    assert touches_audit_prefix(_settings(settings), OPS, "audit/x") is False
