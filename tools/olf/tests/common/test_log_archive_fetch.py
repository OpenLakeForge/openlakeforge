"""The archive reader fetches objects concurrently and answers exactly what a read of one object at a time would."""

from __future__ import annotations

import gzip
import io
import json
import threading
import time
from datetime import date
from typing import Any

import pytest
from openlakeforge_services import logs
from openlakeforge_services.logs import ArchiveCancelled, ArchiveDeadline, ArchivedLogLine, LogArchive, Position

BASE = "s3://ops/activations/dev/logs"
DAY = date(2026, 11, 16)
NEXT_DAY = date(2026, 11, 17)


class _Store:
    """An object store whose GETs take `delay`, and with `jitter` finish in the reverse of the order they started."""

    def __init__(self, delay: float = 0.0, jitter: float = 0.0) -> None:
        self.objects: dict[str, bytes] = {}
        self.failing: dict[str, Exception] = {}
        self.delay = delay
        self.jitter = jitter
        self.fetched: list[str] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self._lock = threading.Lock()

    def put(self, key: str, records: list[dict[str, Any]]) -> None:
        self.objects[key] = gzip.compress("".join(json.dumps(record) + "\n" for record in records).encode())

    def list_objects_v2(  # noqa: N803 - boto3's names
        self, *, Bucket: str, Prefix: str, Delimiter: str = "", ContinuationToken: str = ""
    ) -> dict[str, Any]:
        return {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        with self._lock:
            self.fetched.append(Key)
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
            # Each run of eight GETs ends first-started-last.
            later = 8 - len(self.fetched) % 8
        try:
            time.sleep(self.delay + self.jitter * later)
            if Key in self.failing:
                raise self.failing[Key]
            return {"Body": io.BytesIO(self.objects[Key])}
        finally:
            with self._lock:
                self.in_flight -= 1


def _v2_key(name: str, day: date = DAY) -> str:
    return f"activations/dev/logs/v2/namespace=olf-dev/date={day.isoformat()}/hour=09/{name}.ndjson.gz"


def _v1_key(name: str) -> str:
    return f"activations/dev/logs/k8s/namespace=olf-dev/date={DAY.isoformat()}/hour=09/{name}.ndjson.gz"


def _v2(time: str, message: str, pod: str = "run-1") -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v2",
        "time": time,
        "namespace": "olf-dev",
        "pod": pod,
        "container": "runner",
        "message": message,
        "dedup_key": f"{pod}:{time}:{message}",
    }


def _v1(line: str, pod: str = "run-1") -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v1",
        "archived_at": "2026-11-16T09:30:00+00:00",
        "namespace": "olf-dev",
        "pod": pod,
        "container": "runner",
        "line": line,
    }


def _crowded(store: _Store, objects: int = 40) -> None:
    """Many objects with ties across them, replays of one record in several, v1 copies of v2 lines, and a second day."""
    for n in range(objects):
        records = [_v2(f"2026-11-16T09:{n % 7:02d}:00Z", f"tie {n}"), _v2(f"2026-11-16T09:30:{n:02d}Z", f"own {n}")]
        if n % 5 == 0:
            records.append(_v2("2026-11-16T09:10:00Z", "replayed"))
        store.put(_v2_key(f"dev-{n:02d}"), records)
    store.put(_v2_key("dev-tomorrow", NEXT_DAY), [_v2("2026-11-17T00:00:01Z", "tomorrow")])
    for n in range(12):
        store.put(
            _v1_key(f"k8s-logs-{n:02d}"),
            [_v1(f"2026-11-16T09:30:{n:02d}Z own {n}"), _v1(f"2026-11-16T09:{n % 7:02d}:00Z v1 tie {n}", pod="run-2")],
        )


def _pages(archive: LogArchive, limit: int) -> list[tuple[list[ArchivedLogLine], Position | None]]:
    pages = []
    after: Position | None = None
    for _ in range(200):
        found = archive.read_days("olf-dev", [DAY, NEXT_DAY], limit=limit, after=after)
        pages.append((found.lines, found.last))
        if not found.lines:
            return pages
        after = found.last
    raise AssertionError("the pages did not end")


@pytest.mark.parametrize("limit", [1, 7, 1000])
def test_a_concurrent_read_answers_exactly_what_a_read_of_one_object_at_a_time_does(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    store = _Store(jitter=0.0003)
    _crowded(store)
    concurrent = _pages(LogArchive(store, BASE), limit)
    store.jitter = 0.0
    monkeypatch.setattr(logs, "FETCH_AHEAD", 1)
    one_at_a_time = _pages(LogArchive(store, BASE), limit)

    assert concurrent == one_at_a_time
    lines = [line.line for page, _ in concurrent for line in page]
    assert lines.count("replayed") == 1
    assert len(lines) == len(set(lines)) == 40 * 2 + 1 + 1 + 12
    assert lines[-1] == "tomorrow"


def test_objects_are_fetched_at_once_and_the_read_takes_a_fraction_of_their_sum() -> None:
    delay, objects = 0.05, 48
    store = _Store(delay=delay)
    for n in range(objects):
        store.put(_v2_key(f"dev-{n:02d}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])

    started = time.monotonic()
    lines = LogArchive(store, BASE).read("olf-dev", DAY)
    elapsed = time.monotonic() - started

    assert [line.line for line in lines] == [f"line {n}" for n in range(objects)]
    assert store.most_in_flight == logs.FETCH_AHEAD
    # One at a time this takes objects × delay (2.4 s); FETCH_AHEAD at once, about a sixth of a second per object.
    assert elapsed < objects * delay / 4


def test_a_read_past_its_deadline_starts_no_more_gets() -> None:
    store = _Store(delay=0.01)
    for n in range(40):
        store.put(_v2_key(f"dev-{n:02d}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])
    now = [0.0]
    get_object = store.get_object

    def passing_the_deadline(*, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        now[0] = 10.0
        return get_object(Bucket=Bucket, Key=Key)

    store.get_object = passing_the_deadline  # type: ignore[method-assign]
    archive = LogArchive(store, BASE, clock=lambda: now[0])

    with pytest.raises(ArchiveDeadline):
        archive.read("olf-dev", DAY, deadline=5.0)

    # Only the GETs that had already started when the first one passed the deadline.
    assert len(store.fetched) <= logs.FETCH_AHEAD


def test_a_cancelled_read_stops_before_its_next_object_and_starts_no_more_gets() -> None:
    store = _Store(delay=0.01)
    for n in range(40):
        store.put(_v2_key(f"dev-{n:02d}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])
    cancelled = threading.Event()
    get_object = store.get_object

    def cancelling(*, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        cancelled.set()
        return get_object(Bucket=Bucket, Key=Key)

    store.get_object = cancelling  # type: ignore[method-assign]

    with pytest.raises(ArchiveCancelled):
        LogArchive(store, BASE).read("olf-dev", DAY, cancelled=cancelled.is_set)

    # Only the GETs that had already started when the first one cancelled the read.
    assert len(store.fetched) <= logs.FETCH_AHEAD


def test_a_read_cancelled_before_it_starts_lists_nothing() -> None:
    store = _Store()
    store.put(_v2_key("dev-0"), [_v2("2026-11-16T09:00:00Z", "line 0")])
    listed: list[str] = []
    list_objects = store.list_objects_v2

    def listing(**args: Any) -> dict[str, Any]:
        listed.append(args["Prefix"])
        return list_objects(**args)

    store.list_objects_v2 = listing  # type: ignore[method-assign]

    with pytest.raises(ArchiveCancelled):
        LogArchive(store, BASE).read_days("olf-dev", [DAY], cancelled=lambda: True)

    assert (listed, store.fetched) == ([], [])


def test_an_uncancelled_read_answers_as_a_read_without_the_hook() -> None:
    store = _Store()
    _crowded(store, objects=12)

    with_hook = LogArchive(store, BASE).read_days("olf-dev", [DAY, NEXT_DAY], cancelled=lambda: False)

    assert with_hook == LogArchive(store, BASE).read_days("olf-dev", [DAY, NEXT_DAY])


def test_fetches_queued_behind_another_read_do_not_start_after_their_deadline() -> None:
    # One read holds every fetch thread; a second read's fetches wait for one while its deadline passes.
    release = threading.Event()
    busy = _Store()
    for n in range(logs.FETCH_AHEAD):
        busy.put(_v2_key(f"busy-{n}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"busy {n}")])
    get_busy = busy.get_object
    holding: list[str] = []

    def held(*, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        holding.append(Key)
        release.wait(5)
        return get_busy(Bucket=Bucket, Key=Key)

    busy.get_object = held  # type: ignore[method-assign]
    waiting = _Store()
    for n in range(3):
        waiting.put(_v2_key(f"dev-{n}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])
    now = [0.0]
    outcome: list[Exception | None] = []

    def read_waiting() -> None:
        try:
            LogArchive(waiting, BASE, clock=lambda: now[0]).read("olf-dev", DAY, deadline=5.0)
            outcome.append(None)
        except Exception as exc:
            outcome.append(exc)

    holder = threading.Thread(target=lambda: LogArchive(busy, BASE).read("olf-dev", DAY))
    holder.start()
    for _ in range(500):
        if len(holding) == logs.FETCH_AHEAD:
            break
        time.sleep(0.01)
    assert len(holding) == logs.FETCH_AHEAD
    reader = threading.Thread(target=read_waiting)
    reader.start()
    # Time for the read to list and queue its fetches; were it slower, it would stop before queueing, as it should.
    time.sleep(0.2)
    now[0] = 10.0
    release.set()
    reader.join(5)
    holder.join(5)

    assert len(outcome) == 1 and isinstance(outcome[0], ArchiveDeadline)
    assert waiting.fetched == []


class _Refused(Exception):
    pass


def test_a_failing_get_raises_its_own_error_as_a_read_of_one_object_at_a_time_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(jitter=0.002)
    for n in range(30):
        store.put(_v2_key(f"dev-{n:02d}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])
    # Most GETs fail, the later-started first: the read raises the error of the first failing object in its order.
    for n in range(4, 30):
        store.failing[_v2_key(f"dev-{n:02d}")] = _Refused(f"refused dev-{n:02d}")

    with pytest.raises(_Refused) as concurrent:
        LogArchive(store, BASE).read("olf-dev", DAY)
    monkeypatch.setattr(logs, "FETCH_AHEAD", 1)
    with pytest.raises(_Refused) as one_at_a_time:
        LogArchive(store, BASE).read("olf-dev", DAY)

    assert str(concurrent.value) == str(one_at_a_time.value)


def test_a_body_that_is_not_gzip_fails_the_read_as_before() -> None:
    store = _Store()
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:00Z", "fine")])
    store.objects[_v2_key("dev-b")] = b"not gzip"

    with pytest.raises(OSError):
        LogArchive(store, BASE).read("olf-dev", DAY)


def test_a_day_with_more_objects_than_the_window_still_reads_every_one() -> None:
    store = _Store()
    for n in range(logs.FETCH_AHEAD * 3 + 1):
        store.put(_v2_key(f"dev-{n:02d}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])

    lines = LogArchive(store, BASE).read("olf-dev", DAY)

    assert len(lines) == logs.FETCH_AHEAD * 3 + 1
    assert sorted(store.fetched) == sorted(store.objects)
