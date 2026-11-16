"""The files explorer's upload route: roles, CSRF, single and multipart writes, overwrite races, audit."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from _files_fakes import BUCKETS, DEV, OCTET, OPS, AuditStore, FakeS3, _as, _base, _client, _client_error
from conftest import Mint
from fastapi.testclient import TestClient

from openlakeforge_platform_api import file_writes
from openlakeforge_platform_api.settings import Settings


def upload(api: TestClient, headers: dict[str, str], stage: str = "dev", bucket: str = DEV, **kw: Any) -> Any:
    query = kw.pop("query", "key=new.bin")
    return api.put(
        f"{_base(stage, bucket)}/content?{query}", content=kw.pop("content", b"hello"), headers={**headers, **OCTET}
    )


# roles


@pytest.mark.parametrize("stage", ["dev", "uat", "prod"])
def test_platform_admin_uploads_on_every_stage(api: TestClient, mint: Mint, stage: str) -> None:
    response = upload(api, _as(mint, "platform-admin"), stage, BUCKETS[stage][0])

    assert response.status_code == 201, response.text


def test_data_engineer_uploads_on_dev_and_is_refused_elsewhere(api: TestClient, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")

    assert upload(api, headers).status_code == 201
    refused = upload(api, headers, "uat", "olf-uat-bronze")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "role 'data-engineer' may perform 'upload_object' only on stages: dev"


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_analyst_and_viewer_upload_nothing(api: TestClient, s3: FakeS3, mint: Mint, role: str) -> None:
    assert upload(api, _as(mint, role)).status_code == 403
    assert s3.calls == []


def test_the_ops_bucket_is_writable_by_platform_admin_only(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    assert upload(api, _as(mint, "platform-admin"), bucket=OPS, query="key=notes.txt").status_code == 201
    assert upload(api, _as(mint, "data-engineer"), bucket=OPS, query="key=notes2.txt").status_code == 404
    assert (OPS, "notes2.txt") not in s3.objects


# CSRF


def test_a_cross_origin_upload_is_refused(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    headers = {**_as(mint, "platform-admin"), "Origin": "https://evil.example.com"}

    response = upload(api, headers)

    assert response.status_code == 403 and response.json()["detail"] == "cross-origin write refused"
    assert s3.calls == []


def test_upload_refuses_a_json_body(api: TestClient, mint: Mint) -> None:
    response = api.put(f"{_base()}/content?key=new.bin", json={"a": 1}, headers=_as(mint, "platform-admin"))

    assert response.status_code == 403
    assert response.json()["detail"] == "a request body must be application/octet-stream"


# upload


def test_a_small_upload_is_one_put(api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore) -> None:
    response = upload(api, _as(mint, "data-engineer"), content=b"hello")

    assert response.status_code == 201
    assert response.json() == {"key": "new.bin", "size": 5}
    assert s3.objects[(DEV, "new.bin")][0] == b"hello"
    assert "create_multipart_upload" not in s3.calls
    assert audit_store.phases() == ["attempted", "succeeded"]


def test_a_large_upload_is_multipart_in_parts(
    api: TestClient, s3: FakeS3, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_writes, "PART_BYTES", 4)

    response = upload(api, _as(mint, "data-engineer"), content=b"0123456789")

    assert response.status_code == 201 and response.json()["size"] == 10
    assert s3.part_sizes == [4, 4, 2]
    assert s3.objects[(DEV, "new.bin")][0] == b"0123456789"
    assert "put_object" not in s3.calls and s3.aborted == []


def test_a_body_that_is_an_exact_number_of_parts_has_no_empty_last_part(
    api: TestClient, s3: FakeS3, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_writes, "PART_BYTES", 4)

    assert upload(api, _as(mint, "data-engineer"), content=b"01234567").status_code == 201
    assert s3.part_sizes == [4, 4]


def test_a_multipart_upload_is_aborted_when_a_part_fails(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_writes, "PART_BYTES", 4)
    s3.fail_ops["upload_part"] = lambda: _client_error("InternalError")

    response = upload(api, _as(mint, "data-engineer"), content=b"0123456789")

    assert response.status_code == 502
    assert s3.aborted == ["upload-1"] and s3.uploads == {}
    assert (DEV, "new.bin") not in s3.objects
    assert audit_store.phases() == ["attempted", "failed"]


@pytest.mark.parametrize(("declared", "detail"), [(20, "shorter"), (6, "longer")])
def test_a_body_that_disagrees_with_its_content_length_is_refused_and_aborted(
    api: TestClient, s3: FakeS3, mint: Mint, monkeypatch: pytest.MonkeyPatch, declared: int, detail: str
) -> None:
    monkeypatch.setattr(file_writes, "PART_BYTES", 4)
    headers = {**_as(mint, "data-engineer"), "Content-Length": str(declared)}

    response = upload(api, headers, content=b"0123456789")

    assert response.status_code == 400 and detail in response.json()["detail"]
    assert s3.aborted == ["upload-1"] and (DEV, "new.bin") not in s3.objects


def test_an_upload_without_a_content_length_is_411(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    response = api.put(
        f"{_base()}/content?key=new.bin",
        content=iter([b"ab", b"cd"]),
        headers={**_as(mint, "data-engineer"), **OCTET},
    )

    assert response.status_code == 411
    assert s3.calls == []


def test_an_upload_over_the_cap_is_413_before_anything_is_done(
    settings: Settings, s3: FakeS3, audit_store: AuditStore, mint: Mint
) -> None:
    api = _client(settings, s3, audit_store, files_max_upload_bytes=10)

    assert upload(api, _as(mint, "data-engineer"), content=b"0123456789").status_code == 201
    response = upload(api, _as(mint, "data-engineer"), query="key=big.bin", content=b"0123456789a")

    assert response.status_code == 413
    assert (DEV, "big.bin") not in s3.objects
    assert audit_store.phases() == ["attempted", "succeeded"]  # only the first upload


@pytest.mark.parametrize("key", ["folder/", "../x", "/x", "a/../b", "a%00b"])
def test_an_upload_key_must_be_a_file_key(api: TestClient, s3: FakeS3, mint: Mint, key: str) -> None:
    response = upload(api, _as(mint, "data-engineer"), query=f"key={key}")

    assert response.status_code == 422
    assert s3.calls == []


def test_an_upload_to_a_bucket_of_another_stage_is_404(api: TestClient, mint: Mint) -> None:
    assert upload(api, _as(mint, "data-engineer"), bucket="olf-uat-bronze").status_code == 404


# overwrite


def test_an_existing_object_is_409_unless_overwrite_is_true(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    headers = _as(mint, "data-engineer")

    refused = upload(api, headers, query="key=readme.txt", content=b"new")
    replaced = upload(api, headers, query="key=readme.txt&overwrite=true", content=b"new")

    assert refused.status_code == 409
    assert replaced.status_code == 201
    assert s3.objects[(DEV, "readme.txt")][0] == b"new"


@pytest.mark.parametrize("multipart", [False, True])
def test_an_upload_that_loses_a_race_for_a_new_key_is_409_and_keeps_the_winner(
    api: TestClient, s3: FakeS3, mint: Mint, monkeypatch: pytest.MonkeyPatch, multipart: bool
) -> None:
    if multipart:
        monkeypatch.setattr(file_writes, "PART_BYTES", 4)
    final = "complete_multipart_upload" if multipart else "put_object"
    # The other uploader finishes after this one looked and found nothing, before this one writes.
    s3.hooks[final] = lambda: s3.put(DEV, "race.bin", b"winner")

    response = upload(api, _as(mint, "data-engineer"), query="key=race.bin", content=b"0123456789")

    assert response.status_code == 409
    assert s3.objects[(DEV, "race.bin")][0] == b"winner"
    assert s3.uploads == {} and s3.aborted == (["upload-1"] if multipart else [])


def test_overwrite_true_replaces_whatever_lands_in_between(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    s3.hooks["put_object"] = lambda: s3.put(DEV, "race.bin", b"other")

    response = upload(api, _as(mint, "data-engineer"), query="key=race.bin&overwrite=true", content=b"mine")

    assert response.status_code == 201
    assert s3.objects[(DEV, "race.bin")][0] == b"mine"


def test_two_concurrent_uploads_of_a_new_key_have_exactly_one_winner(api: TestClient, s3: FakeS3, mint: Mint) -> None:
    looked = threading.Barrier(2, timeout=10)

    def both_have_looked() -> None:
        looked.wait()

    s3.hooks["put_object"] = both_have_looked
    headers = _as(mint, "data-engineer")
    statuses: dict[bytes, int] = {}

    def send(body: bytes) -> None:
        statuses[body] = upload(TestClient(api.app), headers, query="key=race.bin", content=body).status_code

    threads = [threading.Thread(target=send, args=(body,)) for body in (b"first", b"second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses.values()) == [201, 409]
    winner = next(body for body, status in statuses.items() if status == 201)
    assert s3.objects[(DEV, "race.bin")][0] == winner


# the event loop


def test_a_slow_audit_store_does_not_stop_the_server_answering_others(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore
) -> None:
    audit_store.hold = release = threading.Event()
    headers = {**_as(mint, "data-engineer"), **OCTET}

    async def scenario() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://api") as client:
            sending = asyncio.create_task(client.put(f"{_base()}/content?key=slow.bin", content=b"x", headers=headers))
            while not audit_store.parked.is_set():
                await asyncio.sleep(0.01)
            health = await client.get("/api/healthz")
            release.set()
            return health.status_code, (await sending).status_code

    health, uploaded = asyncio.run(scenario())

    assert (health, uploaded) == (200, 201)
    assert not audit_store.stalled


def test_the_object_store_calls_of_an_upload_do_not_run_on_the_event_loop(
    api: TestClient, s3: FakeS3, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_writes, "PART_BYTES", 4)
    callers: set[int] = set()
    for name in ("head_object", "create_multipart_upload", "upload_part", "complete_multipart_upload"):
        s3.hooks[name] = lambda: callers.add(threading.get_ident())
    headers = {**_as(mint, "data-engineer"), **OCTET}
    loop_thread: list[int] = []

    async def scenario() -> int:
        loop_thread.append(threading.get_ident())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://api") as client:
            sent = await client.put(f"{_base()}/content?key=big.bin", content=b"0123456789", headers=headers)
            return sent.status_code

    assert asyncio.run(scenario()) == 201
    assert callers and loop_thread[0] not in callers


# cancellation


def _cancel_while_blocked_in(api: TestClient, s3: FakeS3, mint: Mint, call: str, body: bytes = b"x") -> None:
    """Start an upload, hold `call` on the object store, cancel the request, then let `call` finish."""
    gate, entered = threading.Event(), threading.Event()

    def block() -> None:
        entered.set()
        gate.wait(10)

    s3.hooks[call] = block
    headers = {**_as(mint, "data-engineer"), **OCTET}

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://api") as client:
            sending = asyncio.create_task(client.put(f"{_base()}/content?key=cut.bin", content=body, headers=headers))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            sending.cancel()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await sending

    asyncio.run(scenario())


def test_a_request_cancelled_after_the_object_landed_records_success(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore
) -> None:
    _cancel_while_blocked_in(api, s3, mint, "put_object")

    assert (DEV, "cut.bin") in s3.objects
    assert audit_store.phases() == ["attempted", "succeeded"]
    assert list(audit_store.records.values())[-1]["result"] == {"size": "1"}


def test_a_request_cancelled_while_the_multipart_upload_is_created_still_aborts_it(
    api: TestClient, s3: FakeS3, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_writes, "PART_BYTES", 4)

    _cancel_while_blocked_in(api, s3, mint, "create_multipart_upload", body=b"0123456789")

    assert s3.aborted == ["upload-1"]
    assert s3.uploads == {}
    assert (DEV, "cut.bin") not in s3.objects


def test_a_request_cancelled_before_anything_was_written_records_a_failure(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore
) -> None:
    _cancel_while_blocked_in(api, s3, mint, "head_object")

    assert (DEV, "cut.bin") not in s3.objects
    assert audit_store.phases() == ["attempted", "failed"]
    assert "cancelled" in list(audit_store.records.values())[-1]["detail"]


# the audit prefix


AUDIT_KEY = "audit/date=2026-11-01/r.json"
REFUSED_ON_OPS = {
    "upload": "key=audit/x.json",
    "upload over a record": f"key={AUDIT_KEY}&overwrite=true",
}
ALIASES_OF_THE_AUDIT_PREFIX = [
    "audit//platform-api/x",
    "audit\\platform-api\\x",
    "./audit/x",
    "audit/./x",
    "activations/../audit/x",
]


@pytest.mark.parametrize("attempt", list(REFUSED_ON_OPS))
def test_an_upload_may_not_write_to_the_audit_prefix(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore, attempt: str
) -> None:
    before = dict(s3.objects)

    response = upload(api, _as(mint, "platform-admin"), bucket=OPS, query=REFUSED_ON_OPS[attempt])

    assert response.status_code == 403
    assert response.json()["detail"] == "the audit records are read-only"
    assert s3.objects == before
    assert not {"put_object", "complete_multipart_upload"} & set(s3.calls)
    assert audit_store.phases() == ["refused"]


@pytest.mark.parametrize("key", ALIASES_OF_THE_AUDIT_PREFIX)
def test_an_alias_of_the_audit_prefix_is_not_a_key(api: TestClient, s3: FakeS3, mint: Mint, key: str) -> None:
    before = dict(s3.objects)

    response = upload(api, _as(mint, "platform-admin"), bucket=OPS, query="key=" + quote(key, safe=""))

    assert response.status_code == 422
    assert s3.objects == before and s3.calls == []


# the audit record


def test_nothing_changes_when_the_audit_store_is_unavailable(
    api: TestClient, s3: FakeS3, mint: Mint, audit_store: AuditStore
) -> None:
    audit_store.down = True
    before = dict(s3.objects)

    response = upload(api, _as(mint, "data-engineer"))

    assert response.status_code == 503
    assert response.json()["detail"] == "the audit record could not be stored; nothing was done"
    assert s3.objects == before and s3.calls == []
