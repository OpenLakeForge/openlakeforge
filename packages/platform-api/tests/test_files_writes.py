"""The files explorer's folder, rename and delete routes: roles, CSRF, the audit prefix and the audit record."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from _files_fakes import BUCKETS, DEV, OCTET, OPS, AuditStore, FakeS3, _as, _base, _client
from conftest import Mint
from fastapi.testclient import TestClient

from openlakeforge_platform_api.settings import Settings

# An object each stage's first bucket already holds, for the routes that need one to exist.
EXISTING = {"olf-dev-bronze": "readme.txt", "olf-uat-bronze": "u.txt", "olf-prod-bronze": "p.txt", OPS: "notes.txt"}
Send = Callable[..., Any]


def _buckets(stage: str, bucket: str) -> str:
    return f"/api/v1/stages/{stage}/buckets/{bucket}"


def folder(api: TestClient, headers: dict[str, str], stage: str = "dev", bucket: str = DEV, **kw: Any) -> Any:
    return api.post(f"{_buckets(stage, bucket)}/folders", json={"prefix": kw.get("prefix", "new/")}, headers=headers)


def rename(api: TestClient, headers: dict[str, str], stage: str = "dev", bucket: str = DEV, **kw: Any) -> Any:
    body = {"from": kw.get("source", EXISTING[bucket]), "to": kw.get("target", "renamed.txt")}
    return api.post(f"{_base(stage, bucket)}/rename", json=body, headers=headers)


def delete(api: TestClient, headers: dict[str, str], stage: str = "dev", bucket: str = DEV, **kw: Any) -> Any:
    return api.post(
        f"{_base(stage, bucket)}/delete", json={"keys": kw.get("keys", [EXISTING[bucket]])}, headers=headers
    )


ROUTES: dict[str, Send] = {
    "create_folder": folder,
    "rename_object": rename,
    "delete_objects": delete,
}
ALL = pytest.mark.parametrize("operation", list(ROUTES))


# roles


@ALL
@pytest.mark.parametrize("stage", ["dev", "uat", "prod"])
def test_platform_admin_writes_on_every_stage(api: TestClient, mint: Mint, operation: str, stage: str) -> None:
    response = ROUTES[operation](api, _as(mint, "platform-admin"), stage, BUCKETS[stage][0])

    assert response.status_code in (200, 201), response.text


@ALL
def test_data_engineer_writes_on_dev_and_is_refused_elsewhere(api: TestClient, mint: Mint, operation: str) -> None:
    headers = _as(mint, "data-engineer")

    assert ROUTES[operation](api, headers).status_code in (200, 201)
    refused = ROUTES[operation](api, headers, "uat", "olf-uat-bronze")
    assert refused.status_code == 403
    assert refused.json()["detail"] == f"role 'data-engineer' may perform {operation!r} only on stages: dev"


@ALL
@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_analyst_and_viewer_write_nothing(api: TestClient, s3: FakeS3, mint: Mint, operation: str, role: str) -> None:
    assert ROUTES[operation](api, _as(mint, role)).status_code == 403
    assert s3.calls == []


def test_file_capabilities_list_all_seven_operations(api: TestClient, mint: Mint) -> None:
    seven = [
        "list_buckets",
        "list_objects",
        "download_object",
        "upload_object",
        "create_folder",
        "rename_object",
        "delete_objects",
    ]

    engineer = api.get("/api/v1/file-capabilities", headers=_as(mint, "data-engineer")).json()
    admin = api.get("/api/v1/file-capabilities", headers=_as(mint, "platform-admin")).json()

    assert engineer["stages"] == [{"stage": "dev", "operations": seven}]
    assert all(stage["operations"] == seven for stage in admin["stages"])


# CSRF


@ALL
def test_a_cross_origin_write_is_refused(api: TestClient, s3: FakeS3, mint: Mint, operation: str) -> None:
    headers = {**_as(mint, "platform-admin"), "Origin": "https://evil.example.com"}

    response = ROUTES[operation](api, headers)

    assert response.status_code == 403 and response.json()["detail"] == "cross-origin write refused"
    assert s3.calls == []


@pytest.mark.parametrize("path", ["/folders", "/objects/rename", "/objects/delete"])
def test_the_json_routes_refuse_an_octet_stream_body(api: TestClient, mint: Mint, path: str) -> None:
    response = api.post(
        f"{_buckets('dev', DEV)}{path}", content=b"{}", headers={**_as(mint, "platform-admin"), **OCTET}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "a request body must be application/json"


# folders


def test_a_folder_is_a_zero_byte_marker(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    response = folder(api, _as(mint, "data-engineer"), prefix="a/b/")

    assert response.status_code == 201 and response.json() == {"prefix": "a/b/"}
    assert s3.objects[(DEV, "a/b/")][0] == b""


@pytest.mark.parametrize("prefix", ["orders/", "orders/2026/"])
def test_a_folder_where_something_exists_is_409(api: TestClient, s3: FakeS3, mint: Mint, prefix: str) -> None:
    response = folder(api, _as(mint, "data-engineer"), prefix=prefix)

    assert response.status_code == 409
    assert "put_object" not in s3.calls


@pytest.mark.parametrize("prefix", ["a/b", "", "../a/", "/a/"])
def test_a_folder_prefix_must_be_a_non_empty_prefix(api: TestClient, mint: Mint, prefix: str) -> None:
    assert folder(api, _as(mint, "data-engineer"), prefix=prefix).status_code == 422


# rename


def test_a_file_is_renamed(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    response = rename(api, _as(mint, "data-engineer"), source="readme.txt", target="docs/readme.md")

    assert response.status_code == 200 and response.json() == {"moved": 1}
    assert s3.objects[(DEV, "docs/readme.md")][0] == b"hello"
    assert (DEV, "readme.txt") not in s3.objects


def test_a_file_rename_conflicts_and_missing_sources(api: TestClient, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")

    assert rename(api, headers, source="nope.txt", target="x.txt").status_code == 404
    assert rename(api, headers, source="readme.txt", target="orders/2026/a.csv").status_code == 409


@pytest.mark.parametrize(
    ("source", "target"),
    [("a.txt", "b/"), ("a/", "b"), ("a.txt", "a.txt"), ("a/", "a/"), ("a/", "a/b/"), ("../a", "b"), ("a", "")],
)
def test_an_invalid_rename_is_422(api: TestClient, s3: FakeS3, mint: Mint, source: str, target: str) -> None:
    assert rename(api, _as(mint, "data-engineer"), source=source, target=target).status_code == 422
    assert s3.calls == []


def test_a_folder_moves_every_key_under_it(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    response = rename(api, _as(mint, "data-engineer"), source="orders/", target="archive/")

    assert response.status_code == 200 and response.json() == {"moved": 3}
    assert s3.keys() == sorted(["archive/", "archive/2026/a.csv", "archive/2026/b.csv", "readme.txt", 'café "1".txt'])


def test_a_folder_rename_onto_an_existing_folder_or_from_a_missing_one(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")
    s3.put(DEV, "archive/old.txt", b"o")

    assert rename(api, headers, source="orders/", target="archive/").status_code == 409
    assert rename(api, headers, source="ghost/", target="x/").status_code == 404
    assert "copy_object" not in s3.calls


def test_a_folder_of_1000_keys_moves_and_1001_is_422(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")
    for number in range(1000):
        s3.put(DEV, f"big/{number:04}", b"x")

    assert rename(api, headers, source="big/", target="moved/").json() == {"moved": 1000}

    for number in range(1001):
        s3.put(DEV, f"huge/{number:04}", b"x")
    calls = len(s3.calls)
    response = rename(api, headers, source="huge/", target="moved2/")

    assert response.status_code == 422
    assert "copy_object" not in s3.calls[calls:]


def test_a_failed_copy_removes_the_copies_already_made(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore
) -> None:
    before = s3.keys()
    s3.copies_allowed = 2

    response = rename(api, _as(mint, "data-engineer"), source="orders/", target="archive/")

    assert response.status_code == 502
    assert s3.keys() == before
    assert audit_store.phases() == ["attempted", "failed"]


# delete


def test_delete_removes_keys_and_expands_a_prefix(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    response = delete(api, _as(mint, "data-engineer"), keys=["orders/", "readme.txt"])

    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert sorted(body["deleted"]) == ["orders/", "orders/2026/a.csv", "orders/2026/b.csv", "readme.txt"]
    assert s3.keys() == ['café "1".txt']


def test_a_prefix_without_a_marker_still_names_its_marker(api: TestClient, mint: Mint) -> None:
    body = delete(api, _as(mint, "data-engineer"), keys=["orders/2026/"]).json()

    assert sorted(body["deleted"]) == ["orders/2026/", "orders/2026/a.csv", "orders/2026/b.csv"]


def test_a_delete_that_expands_past_1000_keys_is_422_and_deletes_nothing(
    api: TestClient, s3: FakeS3, mint: Mint
) -> None:
    for number in range(1001):
        s3.put(DEV, f"huge/{number:04}", b"x")
    before = s3.keys()

    assert delete(api, _as(mint, "data-engineer"), keys=["huge/"]).status_code == 422
    assert delete(api, _as(mint, "data-engineer"), keys=["huge/", "readme.txt"]).status_code == 422
    assert s3.keys() == before


def test_a_delete_of_1000_keys_is_one_batch_and_1001_is_refused(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")

    ok = delete(api, headers, keys=[f"k{number}" for number in range(1000)])
    too_many = delete(api, headers, keys=[f"k{number}" for number in range(1001)])
    empty = delete(api, headers, keys=[])

    assert ok.status_code == 200 and len(ok.json()["deleted"]) == 1000
    assert s3.calls.count("delete_objects") == 1
    assert too_many.status_code == 422 and empty.status_code == 422


def test_per_key_errors_are_reported_not_raised(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    s3.delete_errors["readme.txt"] = "AccessDenied"

    response = delete(api, _as(mint, "data-engineer"), keys=["readme.txt", 'café "1".txt'])

    assert response.status_code == 200
    assert response.json() == {
        "deleted": ['café "1".txt'],
        "errors": [{"key": "readme.txt", "detail": "AccessDenied"}],
    }


# the audit prefix


AUDIT_KEY = "audit/date=2026-11-01/r.json"
PLATFORM_ADMIN_ON_OPS = {
    "folder under": lambda api, h: folder(api, h, bucket=OPS, prefix="audit/sub/"),
    "folder is the prefix": lambda api, h: folder(api, h, bucket=OPS, prefix="audit/"),
    "rename from": lambda api, h: rename(api, h, bucket=OPS, source=AUDIT_KEY, target="elsewhere.json"),
    "rename into": lambda api, h: rename(api, h, bucket=OPS, source="activations/dev/logs/x.gz", target="audit/z"),
    "rename a folder into": lambda api, h: rename(api, h, bucket=OPS, source="activations/", target="audit/"),
    "delete a record": lambda api, h: delete(api, h, bucket=OPS, keys=["notes.txt", AUDIT_KEY]),
    "delete the folder": lambda api, h: delete(api, h, bucket=OPS, keys=["audit/"]),
}


@pytest.mark.parametrize("attempt", list(PLATFORM_ADMIN_ON_OPS))
def test_nothing_may_write_to_the_audit_prefix(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore, attempt: str
) -> None:
    before = dict(s3.objects)

    response = PLATFORM_ADMIN_ON_OPS[attempt](api, _as(mint, "platform-admin"))

    assert response.status_code == 403
    assert response.json()["detail"] == "the audit records are read-only"
    assert s3.objects == before
    assert not {"put_object", "copy_object", "delete_object", "delete_objects"} & set(s3.calls)
    assert audit_store.phases() == ["refused"]


ALIASES_OF_THE_AUDIT_PREFIX = ["audit//platform-api/x", "audit\\platform-api\\x", "./audit/x"]
ALIAS_ATTEMPTS = {
    "folder": lambda api, h, alias: folder(api, h, bucket=OPS, prefix=alias + "/"),
    "rename from": lambda api, h, alias: rename(api, h, bucket=OPS, source=alias, target="elsewhere.json"),
    "rename into": lambda api, h, alias: rename(api, h, bucket=OPS, source="notes.txt", target=alias),
    "delete": lambda api, h, alias: delete(api, h, bucket=OPS, keys=[alias]),
}


@pytest.mark.parametrize("alias", ALIASES_OF_THE_AUDIT_PREFIX)
@pytest.mark.parametrize("attempt", list(ALIAS_ATTEMPTS))
def test_an_alias_of_the_audit_prefix_is_not_a_key(
    api: TestClient, s3: FakeS3, mint: Mint, attempt: str, alias: str
) -> None:
    before = dict(s3.objects)

    response = ALIAS_ATTEMPTS[attempt](api, _as(mint, "platform-admin"), alias)

    assert response.status_code == 422
    assert s3.objects == before and s3.calls == []


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_a_parent_folder_of_a_nested_audit_prefix_is_refused(
    settings: Settings, s3: FakeS3, audit_store: AuditStore, mint: Mint, operation: str
) -> None:
    api = _client(settings, s3, audit_store, audit_base_uri=f"s3://{OPS}/audit/platform-api/")
    headers = _as(mint, "platform-admin")

    if operation == "rename":
        response = rename(api, headers, bucket=OPS, source="audit/", target="old/")
    else:
        response = delete(api, headers, bucket=OPS, keys=["audit/"])

    assert response.status_code == 403
    assert not {"copy_object", "delete_object", "delete_objects"} & set(s3.calls)


# the audit record


def test_each_write_stores_who_what_and_how_it_ended(api: TestClient, mint: Mint, audit_store: AuditStore) -> None:
    rename(api, _as(mint, "data-engineer"), source="readme.txt", target="docs/readme.md")

    attempted, succeeded = (audit_store.records[key] for key in sorted(audit_store.records))
    assert attempted["operation"] == "rename_object" and attempted["phase"] == "attempted"
    assert attempted["stage"] == "dev" and attempted["role"] == "data-engineer"
    assert attempted["target"] == {"bucket": DEV, "from": "readme.txt", "to": "docs/readme.md"}
    assert succeeded["phase"] == "succeeded" and succeeded["result"] == {"moved": "1"}


def test_a_delete_target_is_a_compact_description(api: TestClient, mint: Mint, audit_store: AuditStore) -> None:
    delete(api, _as(mint, "data-engineer"), keys=["a1", "a2", "a3", "a4", "a5"])

    attempted = audit_store.records[sorted(audit_store.records)[0]]
    assert attempted["target"] == {"bucket": DEV, "keys": "a1, a2, a3 (+2 more)"}


@ALL
def test_nothing_changes_when_the_audit_store_is_unavailable(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore, operation: str
) -> None:
    audit_store.down = True
    before = dict(s3.objects)

    response = ROUTES[operation](api, _as(mint, "data-engineer"))

    assert response.status_code == 503
    assert response.json()["detail"] == "the audit record could not be stored; nothing was done"
    assert s3.objects == before and s3.calls == []
