"""Both log routes on the shared read workers: a client that goes away stops its read, and a busy pool answers fast.

The TestClient never reports a disconnect while a request runs, so the disconnect cases drive the app as an ASGI
server would, with a `receive` that answers `http.disconnect` once the test says the client is gone.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from conftest import Mint
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openlakeforge_services.logs import FETCH_AHEAD
from test_logs import DEV_BASE, ObjectStore, _v2
from test_reads import FakeDagster, _as, _settings
from test_run_logs import RUN_ID, _run

from openlakeforge_platform_api import log_reads
from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings

DAY = "2026-10-31"
OBJECTS = 3 * FETCH_AHEAD
DAY_LOGS = f"/api/v1/stages/dev/logs?date={DAY}&hour=0"
RUN_LOGS = f"/api/v1/stages/dev/runs/{RUN_ID}/logs"


class GatedStore(ObjectStore):
    """Every GET is recorded, then waits for `gate`, then for `delay` seconds."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.delay = 0.0
        self._lock = threading.Lock()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        with self._lock:
            answer = super().get_object(Bucket=Bucket, Key=Key)
        self.gate.wait(timeout=30)
        time.sleep(self.delay)
        return answer

    def gets(self) -> int:
        with self._lock:
            return len(self.opened)


@pytest.fixture
def store() -> GatedStore:
    fake = GatedStore()
    for n in range(OBJECTS):
        fake.archive(
            f"activations/dev/logs/v2/namespace=olf-dev/date={DAY}/hour=00/dev-{n:02d}.ndjson.gz",
            [_v2(f"{DAY}T00:05:{n:02d}Z", f"INFO line {n}", run_id=RUN_ID)],
        )
    return fake


@pytest.fixture
def dagster() -> Iterator[FakeDagster]:
    fake = FakeDagster()
    fake.runs = [_run(f"{DAY}T00:00:00+00:00", f"{DAY}T00:30:00+00:00")]
    yield fake
    fake.close()


@pytest.fixture
def app(settings: Settings, dagster: FakeDagster, store: GatedStore) -> FastAPI:
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
        log_archives={"dev": {"base_uri": DEV_BASE, "namespace": "olf-dev"}},
    )
    return create_app(configured, object_store=store)


@dataclass
class Call:
    """One request as an ASGI server makes it; `leave()` is the client closing the connection."""

    gone: asyncio.Event = field(default_factory=asyncio.Event)
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    answered_at: float | None = None

    def leave(self) -> None:
        self.gone.set()


async def _call(app: FastAPI, path: str, headers: dict[str, str], call: Call) -> None:
    url = urlsplit(path)
    sent_request = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await call.gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            call.status = message["status"]
            call.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            call.answered_at = time.monotonic()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": url.path,
        "raw_path": url.path.encode(),
        "query_string": url.query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver"), *((k.lower().encode(), v.encode()) for k, v in headers.items())],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)


async def _until(condition: Any, seconds: float = 10.0) -> None:
    ends = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= ends:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("path", [DAY_LOGS, RUN_LOGS])
def test_a_client_that_goes_away_stops_its_read_and_frees_its_worker(
    app: FastAPI, store: GatedStore, mint: Mint, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(log_reads, "QUEUE_WAIT_SECONDS", 2.0)
    headers = _as(mint, "data-engineer")

    async def scenario() -> None:
        # One read per worker, each stuck on its first GETs.
        calls = [Call(), Call()]
        tasks = [asyncio.create_task(_call(app, path, headers, call)) for call in calls]
        await _until(lambda: store.gets() >= 2)
        for call in calls:
            call.leave()
        # Answered while every GET is still held: the handler waits on nothing once the client is gone.
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert [call.status for call in calls] == [log_reads.CLIENT_CLOSED_REQUEST] * 2
        store.gate.set()
        await asyncio.sleep(0.5)

    asyncio.run(scenario())

    # Only the GETs already in flight finished; neither read went on to the rest of its objects.
    assert store.gets() <= FETCH_AHEAD < 2 * OBJECTS
    response = TestClient(app).get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert len(response.json()["lines"]) == OBJECTS


def test_a_read_that_waits_past_the_queue_bound_is_answered_503_with_retry_after(
    app: FastAPI, store: GatedStore, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(log_reads, "QUEUE_WAIT_SECONDS", 0.3)
    headers = _as(mint, "data-engineer")

    async def scenario() -> list[Call]:
        busy = [Call(), Call()]
        tasks = [asyncio.create_task(_call(app, DAY_LOGS, headers, call)) for call in busy]
        await _until(lambda: store.gets() >= 2)
        opened = store.gets()
        queued = Call()
        asked = time.monotonic()
        await asyncio.wait_for(_call(app, DAY_LOGS, headers, queued), timeout=5)
        assert queued.answered_at is not None and queued.answered_at - asked < 2
        # It never reached the store.
        assert store.gets() == opened
        store.gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        return [*busy, queued]

    first, second, queued = asyncio.run(scenario())

    assert (first.status, second.status) == (200, 200)
    assert queued.status == 503
    assert queued.headers["retry-after"] == str(log_reads.RETRY_AFTER_SECONDS)


def test_time_spent_queued_does_not_count_against_the_read_deadline(
    app: FastAPI, store: GatedStore, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each read takes one GET's 1.6 s, 0.8 s inside the 2.4 s deadline; the third waits about 1.6 s for a worker first,
    # so its whole request takes about 3.2 s, 0.8 s past a deadline counted from its arrival.
    monkeypatch.setattr(log_reads, "READ_DEADLINE_SECONDS", 2.4)
    monkeypatch.setattr(log_reads, "QUEUE_WAIT_SECONDS", 3.0)
    for key in [key for _, key in store.objects][1:]:
        del store.objects[("ops", key)]
    store.gate.set()
    store.delay = 1.6
    headers = _as(mint, "data-engineer")

    async def scenario() -> tuple[list[Call], float]:
        calls = [Call(), Call(), Call()]
        tasks = [asyncio.create_task(_call(app, DAY_LOGS, headers, call)) for call in calls[:2]]
        await _until(lambda: store.gets() >= 2)
        asked = time.monotonic()
        await asyncio.wait_for(_call(app, DAY_LOGS, headers, calls[2]), timeout=10)
        await asyncio.gather(*tasks)
        assert calls[2].answered_at is not None
        return calls, calls[2].answered_at - asked

    calls, took = asyncio.run(scenario())

    assert [call.status for call in calls] == [200, 200, 200]
    assert took > log_reads.READ_DEADLINE_SECONDS


def test_a_read_slower_than_the_deadline_is_answered_502(
    app: FastAPI, store: GatedStore, mint: Mint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(log_reads, "READ_DEADLINE_SECONDS", 0.3)
    store.gate.set()
    store.delay = 1.0

    started = time.monotonic()
    response = TestClient(app).get(DAY_LOGS, headers=_as(mint, "data-engineer"))

    assert response.status_code == 502
    assert response.json()["detail"] == "stage 'dev''s log archive did not answer in 0.3 s"
    assert time.monotonic() - started < 1.0


class _Leaving:
    """A request whose client leaves while the read finishes: `is_disconnected` answers only once the read is done."""

    def __init__(self, done: threading.Event) -> None:
        self._done = done

    async def is_disconnected(self) -> bool:
        while not self._done.is_set():
            await asyncio.sleep(0.01)
        # The worker sets the event just before returning; this lets its result reach the future first.
        await asyncio.sleep(0.05)
        return True


def test_a_read_that_finishes_as_the_client_leaves_is_answered_not_discarded() -> None:
    done = threading.Event()

    def read(_slot: log_reads.ReadSlot) -> str:
        done.set()
        return "lines"

    answer = asyncio.run(log_reads.bounded_read(cast(Any, _Leaving(done)), read, waiting_on="the archive"))

    assert answer == "lines"


def test_a_read_that_fails_as_the_client_leaves_raises_its_own_error() -> None:
    done = threading.Event()

    def read(_slot: log_reads.ReadSlot) -> str:
        done.set()
        raise HTTPException(status_code=502, detail="the object store is unreachable")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(log_reads.bounded_read(cast(Any, _Leaving(done)), read, waiting_on="the archive"))

    assert (raised.value.status_code, raised.value.detail) == (502, "the object store is unreachable")
