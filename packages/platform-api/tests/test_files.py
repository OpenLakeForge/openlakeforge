"""The files explorer's read routes against an in-memory S3, with the role matrix, bucket and key checks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from _files_fakes import BUCKETS, OPS, WHEN, FakeS3, _as, _base, _client_error, _settings
from botocore.exceptions import EndpointConnectionError
from conftest import Mint
from fastapi.testclient import TestClient

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.files import touches_audit_prefix
from openlakeforge_platform_api.settings import Settings

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
    read = ["list_buckets", "list_objects", "download_object"]

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


@pytest.mark.parametrize("prefix", ["orders", "/orders/", "a/../b/", "a/../", "bad\x01/", "a//b/", "a\\b/", "./a/"])
def test_an_invalid_prefix_is_refused(api: TestClient, mint: Mint, prefix: str) -> None:
    response = api.get(_base(), params={"prefix": prefix}, headers=_as(mint, "data-engineer"))

    assert response.status_code == 422


def test_a_listing_honours_the_page_size_and_continues_after_it(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    for number in range(5):
        s3.put("olf-dev-bronze", f"logs/f{number}.txt", b"x")
    s3.put("olf-dev-bronze", "logs/", b"")
    s3.put("olf-dev-bronze", "logs/sub/deep.txt", b"x")
    s3.put("olf-dev-bronze", "logs-other/y.txt", b"x")
    headers = _as(mint, "data-engineer")
    pages: list[dict[str, Any]] = []
    cursor = ""
    while True:
        body = api.get(_base(), params={"prefix": "logs/", "limit": 2, "cursor": cursor}, headers=headers).json()
        pages.append(body)
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]

    assert len(pages) > 1
    assert all(len(page["files"]) + len(page["folders"]) <= 2 for page in pages)
    assert [file["key"] for page in pages for file in page["files"]] == [f"logs/f{number}.txt" for number in range(5)]
    assert [folder["prefix"] for page in pages for folder in page["folders"]] == ["logs/sub/"]


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


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/abs",
        "a/../b",
        "..",
        "a/..",
        "a\x00b",
        "a\nb",
        "k" * 1025,
        "é" * 513,
        "audit//platform-api/x",
        "a//",
        "audit\\platform-api\\x",
        "./audit/x",
        "a/./b",
        "a/.",
    ],
)
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
        (OPS, "audit//platform-api/x", True),
        (OPS, "audit\\platform-api\\x", True),
        (OPS, "./audit/x", True),
        (OPS, "activations/../audit/x", True),
        (OPS, "audit/..", True),
        (OPS, "a//", False),
        (OPS, "activations//x", False),
    ],
)
def test_touches_audit_prefix(settings: Settings, bucket: str, key: str, touches: bool) -> None:
    configured = _settings(settings, audit_base_uri=f"s3://{OPS}/audit/")

    assert touches_audit_prefix(configured, bucket, key) is touches


def test_a_deployment_without_audit_has_no_protected_prefix(settings: Settings) -> None:
    assert touches_audit_prefix(_settings(settings), OPS, "audit/x") is False
