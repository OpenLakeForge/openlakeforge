"""The archive reader on collector (v2) objects, alone and beside the polling archiver's (v1)."""

from __future__ import annotations

import gzip
import io
import json
import logging
import weakref
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from openlakeforge_core.log_archive import V1_LOOKBACK_SECONDS
from openlakeforge_services.logs import UPLOAD_CLOCK_SLACK, ArchiveDeadline, LogArchive, level_of

BASE = "s3://ops/activations/dev/logs"
DAY = date(2026, 11, 16)


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        # LastModified as boto3 lists it, for the objects given one; the others are listed without it.
        self.modified: dict[str, datetime] = {}
        self.listed: list[str] = []
        self.opened: list[str] = []

    def put(self, key: str, records: list[dict[str, Any]], modified: datetime | None = None) -> None:
        self.objects[key] = gzip.compress("".join(json.dumps(record) + "\n" for record in records).encode())
        if modified is not None:
            self.modified[key] = modified

    def list_objects_v2(self, *, Bucket: str, Prefix: str, ContinuationToken: str = "") -> dict[str, Any]:  # noqa: N803
        self.listed.append(Prefix)
        return {
            "Contents": [
                {"Key": key, **({"LastModified": self.modified[key]} if key in self.modified else {})}
                for key in sorted(self.objects)
                if key.startswith(Prefix)
            ]
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.opened.append(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def _v2(time: str, message: str, **fields: Any) -> dict[str, Any]:
    # The fields the collector's record builder writes (log-collector/main.tf); `missing` is always an array.
    record = {
        "schema": "openlakeforge.k8s_log.v2",
        "time": time,
        "date": time[:10],
        "stage": "dev",
        "namespace": "olf-dev",
        "pod": "dagster-run-8f1c2d4e-0b7a-4c55-9a10-2f6e7a3b9c01-x7k2p",
        "container": "runner",
        "container_id": "3f9c0a41d5e2",
        "stream": "stderr",
        "message": message,
        "missing": ["product"],
        "dedup_key": f"3f9c0a41d5e2:{time}:{message}",
    }
    return {**record, **fields}


def _v2_key(name: str, namespace: str = "olf-dev", day: str = "2026-11-16") -> str:
    return f"activations/dev/logs/v2/namespace={namespace}/date={day}/{name}.ndjson.gz"


def _v1_key(hour: str = "11") -> str:
    return f"activations/dev/logs/k8s/namespace=olf-dev/date=2026-11-16/hour={hour}/k8s-logs-x.ndjson.gz"


def _v1(line: str, pod: str = "run-1") -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v1",
        "archived_at": "2026-11-16T11:00:00+00:00",
        "namespace": "olf-dev",
        "pod": pod,
        "container": "runner",
        "line": line,
    }


@pytest.fixture
def store() -> _Store:
    return _Store()


def _read(store: _Store, **filters: Any) -> list[str]:
    return [line.line or "" for line in LogArchive(store, BASE).read("olf-dev", DAY, **filters)]


def test_v2_lines_come_back_in_time_order_across_objects_with_their_fields(store: _Store) -> None:
    store.put(_v2_key("dev-b"), [_v2("2026-11-16T10:00:00.5Z", "late")])
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:59:59.999999999Z", "early", run_id="r1", component="floe")])

    first, second = LogArchive(store, BASE).read("olf-dev", DAY)

    assert (first.line, second.line) == ("early", "late")
    assert (first.run_id, first.component, first.stream, first.source) == ("r1", "floe", "stderr", "v2")
    assert second.missing == ("product",)


def test_a_record_replayed_after_a_restart_is_returned_once_across_a_partition_boundary(store: _Store) -> None:
    # The same line was flushed into two objects an hour apart; neither it nor its neighbours may be lost.
    replayed = _v2("2026-11-16T09:59:59.9Z", "boundary")
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:00Z", "before"), replayed])
    store.put(_v2_key("dev-b"), [replayed, _v2("2026-11-16T10:00:00Z", "after")])

    assert _read(store) == ["before", "boundary", "after"]
    assert _read(store, hour=9) == ["before", "boundary"]
    assert _read(store, hour=10) == ["after"]


def test_lines_are_selected_by_run_product_component_severity_and_time(store: _Store) -> None:
    store.put(
        _v2_key("dev-a"),
        [
            _v2("2026-11-16T09:00:00Z", "INFO start", run_id="r1", product="sales", component="floe"),
            _v2("2026-11-16T09:01:00Z", "ERROR contract violated", run_id="r1", product="sales", component="floe"),
            _v2("2026-11-16T09:02:00Z", "ERROR elsewhere", run_id="r2", product="hr", component="dbt"),
            _v2("2026-11-16T09:03:00Z", "no level here", run_id="r1", product="sales", component="floe"),
        ],
    )

    assert _read(store, run_id="r1", severity="error") == ["ERROR contract violated"]
    assert _read(store, product="hr") == ["ERROR elsewhere"]
    assert _read(store, component="floe", severity="info", limit=2) == ["INFO start", "ERROR contract violated"]
    assert _read(store, since=datetime(2026, 11, 16, 9, 2, tzinfo=UTC)) == ["ERROR elsewhere", "no level here"]
    assert _read(store, until=datetime(2026, 11, 16, 9, 1)) == ["INFO start"]


def test_only_the_stages_own_namespace_and_day_are_listed_or_read(store: _Store) -> None:
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:00Z", "mine")])
    store.put(_v2_key("prod-a", namespace="olf-prod"), [_v2("2026-11-16T09:00:00Z", "x", namespace="olf-prod")])
    store.put(_v2_key("dev-b", day="2026-11-17"), [_v2("2026-11-17T00:00:00Z", "tomorrow")])

    assert _read(store) == ["mine"]
    assert set(store.listed) == {
        "activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/",
        "activations/dev/logs/k8s/namespace=olf-dev/date=2026-11-16/",
    }


def test_a_record_of_another_namespace_in_the_partition_is_skipped(store: _Store) -> None:
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:00Z", "stray", namespace="olf-prod")])

    assert _read(store) == []


@pytest.mark.parametrize("namespace", ["olf-dev/date=2026-11-16", "..", "olf-dev=x", "OLF", "", "olf.dev"])
def test_a_namespace_that_is_not_a_kubernetes_name_never_reaches_a_key(store: _Store, namespace: str) -> None:
    with pytest.raises(ValueError):
        LogArchive(store, BASE).read(namespace, DAY)

    assert store.listed == []


def test_a_severity_outside_the_set_is_refused(store: _Store) -> None:
    with pytest.raises(ValueError, match="severity"):
        LogArchive(store, BASE).read("olf-dev", DAY, severity="error/..")


def test_a_v1_line_is_dropped_only_when_v2_holds_the_same_line_and_otherwise_kept(store: _Store) -> None:
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:01Z", "seen twice", pod="run-1")])
    store.put(
        _v1_key(),
        [
            _v1("2026-11-16T08:00:00Z from before the collector"),
            _v1("2026-11-16T09:00:01Z seen twice"),
            _v1("2026-11-16T09:00:01Z seen twice", pod="run-2"),
        ],
    )

    lines = LogArchive(store, BASE).read("olf-dev", DAY)

    assert [(line.source, line.pod) for line in lines] == [("v1", "run-1"), ("v2", "run-1"), ("v1", "run-2")]


def test_run_scoped_filters_leave_v1_out_because_it_cannot_carry_them(store: _Store) -> None:
    store.put(_v1_key(), [_v1("x")])

    assert _read(store, run_id="r1") == []
    assert _read(store) == ["x"]


def test_a_v1_and_a_v2_event_in_the_same_microsecond_sort_by_their_nanoseconds(store: _Store) -> None:
    # The v1 line was archived two hours after the v2 line was written; archive time must not decide.
    store.put(_v1_key(), [_v1("2026-11-16T09:00:00.123456001Z earlier")])
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:00.123456999Z", "later")])

    assert _read(store, limit=1) == ["2026-11-16T09:00:00.123456001Z earlier"]
    assert _read(store) == ["2026-11-16T09:00:00.123456001Z earlier", "later"]


def test_dedup_state_stays_proportional_to_the_limit_not_to_the_day(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Tracked(str):
        """A dedup key that can be counted while anything still holds it."""

    live: weakref.WeakSet[Tracked] = weakref.WeakSet()
    most_alive = 0
    real_loads = json.loads

    def counting_loads(raw: str) -> Any:
        nonlocal most_alive
        record = real_loads(raw)
        if isinstance(record.get("dedup_key"), str):
            record["dedup_key"] = Tracked(record["dedup_key"])
            live.add(record["dedup_key"])
            most_alive = max(most_alive, len(live))
        return record

    monkeypatch.setattr("openlakeforge_services.logs.json.loads", counting_loads)
    # Descending times, so every record beats the one kept so far and is offered to the candidates.
    records = [_v2(f"2026-11-16T09:{59 - n // 60:02d}:{59 - n % 60:02d}Z", f"line {n}") for n in range(3000)]
    store.put(_v2_key("dev-a"), records[:1500])
    store.put(_v2_key("dev-b"), records[1500:])

    assert _read(store, limit=1) == ["line 2999"]
    assert most_alive <= 5


def test_several_days_read_as_one_ordering_with_one_dedup(store: _Store) -> None:
    # A replay can land in the next day's object only if its time is that day's, so it is the same day here; the
    # dedup and the limit still run over both days, not per day.
    replayed = _v2("2026-11-16T23:59:59.9Z", "before midnight")
    store.put(_v2_key("dev-a"), [replayed, replayed])
    store.put(_v2_key("dev-b", day="2026-11-17"), [_v2("2026-11-17T00:00:00.1Z", "after midnight")])
    store.put(
        "activations/dev/logs/k8s/namespace=olf-dev/date=2026-11-17/hour=00/k8s-logs-x.ndjson.gz",
        [_v1("2026-11-17T00:00:00.05Z from v1")],
    )
    archive = LogArchive(store, BASE)

    both = archive.read_days("olf-dev", [date(2026, 11, 17), DAY]).lines

    assert [line.line for line in both] == ["before midnight", "2026-11-17T00:00:00.05Z from v1", "after midnight"]
    assert [line.timestamp for line in both] == [
        "2026-11-16T23:59:59.9Z",
        "2026-11-17T00:00:00.05Z",
        "2026-11-17T00:00:00.1Z",
    ]
    assert [line.line for line in archive.read_days("olf-dev", [DAY, date(2026, 11, 17)], limit=1).lines] == [
        "before midnight"
    ]


@pytest.mark.parametrize(
    ("message", "level"),
    [
        ("ERROR contract violated", "error"),
        ("2026-11-16 09:41:07 WARNING disk", "warning"),
        ("2026-11-16T09:41:07.123Z\tINFO\tmain\tstarted", "info"),
        ("2026-11-16 09:41:07 +0000 - dagster - DEBUG - orders_job - step", "debug"),
        ("26/11/16 09:41:07 FATAL shutdown", "critical"),
        ("[2026/11/16 09:41:07] [ warn] [output:s3] retry", "warning"),
        ("ERROR:root:boom", "error"),
        ('{"level": "ERROR", "msg": "x"}', "error"),
        ('{"severity": "info"}', "info"),
        ('{"levelname": "WARNING"}', "warning"),
        ("ts=2026-11-16T09:41:07Z level=error msg=x", "error"),
        ('ts=1 level="warn" msg="a \\"quoted\\" word"', "warning"),
        # Only a top-level key counts: a `level=` inside a quoted value is that value's text.
        ('msg="retry after level=error again" level=info', "info"),
        ('msg="example level=critical token"', None),
        # An unbalanced quote is not logfmt, so nothing in the line is trusted.
        ('msg="open level=error', None),
        # Stated nowhere the reader recognises: no level, rather than a guess.
        ("Error occurred while executing op", None),
        ("retrying after error", None),
        ("ERRORS: 3", None),
        ('{"level": 30}', None),
        ('{"level": "notice"}', None),
        ("{not json ERROR", None),
        ("Traceback (most recent call last):", None),
        ("", None),
        (None, None),
    ],
)
def test_a_level_is_read_only_where_the_message_states_it(message: str | None, level: str | None) -> None:
    assert level_of(message) == level


def test_a_v1_line_keeps_its_kubelet_time_and_an_error_record_has_none(store: _Store) -> None:
    store.put(
        _v1_key(),
        [
            _v1("2026-11-16T11:00:00.5Z ERROR failed"),
            {**_v1(""), "line": None, "archive_error": "container not found"},
        ],
    )

    lines = LogArchive(store, BASE).read("olf-dev", DAY)

    assert [(line.timestamp, line.level, line.error) for line in lines] == [
        (None, None, "container not found"),
        ("2026-11-16T11:00:00.5Z", "error", None),
    ]


def test_v1_is_listed_past_the_window_by_the_archivers_lookback_and_still_filtered_by_event_time(store: _Store) -> None:
    # The archiver ran at 00:00:05 and read the hour before, so the 23:59 line sits in the next day's partition.
    store.put(
        "activations/dev/logs/k8s/namespace=olf-dev/date=2026-11-17/hour=00/k8s-logs-x.ndjson.gz",
        [
            {**_v1("2026-11-16T23:59:10Z ERROR last"), "archived_at": "2026-11-17T00:00:05+00:00"},
            {**_v1("2026-11-17T00:00:01Z later"), "archived_at": "2026-11-17T00:00:05+00:00"},
        ],
    )
    archive = LogArchive(store, BASE)
    until = datetime(2026, 11, 16, 23, 59, 30, tzinfo=UTC)

    assert archive.read_days("olf-dev", [DAY], until=until).lines == []
    found = archive.read_days("olf-dev", [DAY], until=until, v1_lookback=timedelta(seconds=V1_LOOKBACK_SECONDS))

    assert [line.line for line in found.lines] == ["2026-11-16T23:59:10Z ERROR last"]


def test_a_scan_past_its_deadline_stops_before_reading_the_rest(store: _Store) -> None:
    for n in range(20):
        store.put(_v2_key(f"dev-{n:02d}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])
    fetched: list[str] = []
    get_object = store.get_object

    def counting(*, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        fetched.append(Key)
        return get_object(Bucket=Bucket, Key=Key)

    store.get_object = counting  # type: ignore[method-assign]
    # Every look at the clock moves it one second on.
    ticks = iter(range(1000))
    archive = LogArchive(store, BASE, clock=lambda: float(next(ticks)))

    with pytest.raises(ArchiveDeadline):
        archive.read("olf-dev", DAY, deadline=5.0)

    assert len(fetched) <= 3


def test_a_line_in_both_layouts_is_answered_once_when_a_page_ends_on_its_v2_record(store: _Store) -> None:
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:01Z", "seen twice", pod="run-1")])
    store.put(_v1_key(), [_v1("2026-11-16T09:00:01Z seen twice")])
    archive = LogArchive(store, BASE)

    first = archive.read_days("olf-dev", [DAY], limit=1)
    second = archive.read_days("olf-dev", [DAY], limit=1, after=first.last)

    assert [line.source for line in first.lines] == ["v2"]
    assert second.lines == []


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=UTC)


def test_an_object_uploaded_before_the_window_is_not_opened_and_the_edges_and_late_uploads_are(
    store: _Store, caplog: pytest.LogCaptureFixture
) -> None:
    since = _at("2026-11-16T10:00:00")
    # Uploaded a second before the slack ends: everything in it was written before the window.
    store.put(
        _v2_key("dev-old"),
        [_v2("2026-11-16T09:40:00Z", "old")],
        modified=since - UPLOAD_CLOCK_SLACK - timedelta(seconds=1),
    )
    # The store's clock runs the whole slack behind the node's: the object is still read.
    store.put(_v2_key("dev-edge"), [_v2("2026-11-16T10:00:00Z", "at the start")], modified=since - UPLOAD_CLOCK_SLACK)
    # Spooled through an outage and uploaded hours after the window: no upper bound drops it.
    store.put(_v2_key("dev-late"), [_v2("2026-11-16T10:30:00Z", "spooled")], modified=_at("2026-11-16T14:00:00"))
    store.put(_v2_key("dev-unknown"), [_v2("2026-11-16T10:01:00Z", "no LastModified")])
    store.put(_v1_key("09"), [_v1("2026-11-16T09:30:00Z old v1")], modified=_at("2026-11-16T09:31:00"))
    store.put(_v1_key("10"), [_v1("2026-11-16T10:02:00Z v1")], modified=_at("2026-11-16T10:03:00"))
    archive = LogArchive(store, BASE)

    with caplog.at_level(logging.INFO, logger="openlakeforge_services.logs"):
        found = archive.read_days("olf-dev", [DAY], since=since, until=_at("2026-11-16T11:00:00"))

    assert [line.line for line in found.lines] == [
        "at the start",
        "no LastModified",
        "2026-11-16T10:02:00Z v1",
        "spooled",
    ]
    assert sorted(store.opened) == sorted(
        [_v2_key("dev-edge"), _v2_key("dev-late"), _v2_key("dev-unknown"), _v1_key("10")]
    )
    assert "6 objects listed, 4 read" in caplog.text
    assert "old" not in caplog.text


def test_without_a_start_every_object_is_still_read(store: _Store) -> None:
    store.put(_v2_key("dev-old"), [_v2("2026-11-16T09:40:00Z", "old")], modified=_at("2026-11-16T09:41:00"))

    assert _read(store) == ["old"]
    assert store.opened == [_v2_key("dev-old")]


def test_a_window_across_midnight_reads_the_run_on_both_days_and_skips_the_day_before_it(store: _Store) -> None:
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T20:00:00Z", "evening")], modified=_at("2026-11-16T20:01:00"))
    store.put(_v2_key("dev-b"), [_v2("2026-11-16T23:58:30Z", "before midnight")], modified=_at("2026-11-16T23:59:30"))
    store.put(
        _v2_key("dev-c", day="2026-11-17"),
        [_v2("2026-11-17T00:01:00Z", "after midnight")],
        modified=_at("2026-11-17T00:04:00"),
    )
    archive = LogArchive(store, BASE)

    found = archive.read_days(
        "olf-dev", [DAY, date(2026, 11, 17)], since=_at("2026-11-16T23:58:00"), until=_at("2026-11-17T00:03:00")
    )

    assert [line.line for line in found.lines] == ["before midnight", "after midnight"]
    assert _v2_key("dev-a") not in store.opened


def test_paging_with_a_start_that_follows_the_cursor_returns_every_line_once(store: _Store) -> None:
    tie = "2026-11-16T10:05:00Z"
    replayed = _v2(tie, "tied b")
    store.put(_v2_key("dev-old"), [_v2("2026-11-16T08:00:00Z", "old")], modified=_at("2026-11-16T08:01:00"))
    store.put(
        _v2_key("dev-a"),
        [_v2("2026-11-16T10:01:00Z", "first"), _v2(tie, "tied a"), replayed],
        modified=_at("2026-11-16T10:06:00"),
    )
    # A collector replay of a record in another object, and a v1 copy of a v2 line.
    store.put(_v2_key("dev-b"), [replayed, _v2(tie, "tied c")], modified=_at("2026-11-16T10:06:00"))
    store.put(_v2_key("dev-c"), [_v2("2026-11-16T10:40:00Z", "late", pod="run-1")], modified=_at("2026-11-16T10:41:00"))
    store.put(
        _v1_key("10"),
        [_v1("2026-11-16T10:40:00Z late"), _v1("2026-11-16T10:20:00Z v1 only")],
        modified=_at("2026-11-16T11:00:00"),
    )
    archive = LogArchive(store, BASE)
    start, until = _at("2026-11-16T10:00:00"), _at("2026-11-16T11:00:00")

    seen: list[str] = []
    after = None
    for _ in range(20):
        # As the run logs route does: the window starts at the cursor's microsecond, so the bound moves with it.
        since = (
            start
            if after is None
            else max(start, datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=after[0] // 1000))
        )
        page = archive.read_days("olf-dev", [DAY], since=since, until=until, limit=1, after=after)
        seen += [line.line or "" for line in page.lines]
        if not page.lines:
            break
        after = page.last

    # Ties sort by object, which is a digest of its key, so only their set is fixed.
    assert (seen[0], sorted(seen[1:4]), seen[4:]) == (
        "first",
        ["tied a", "tied b", "tied c"],
        ["2026-11-16T10:20:00Z v1 only", "late"],
    )
    assert _v2_key("dev-old") not in store.opened


def test_the_deadline_still_stops_a_long_scan_of_objects_inside_the_window(store: _Store) -> None:
    for n in range(20):
        store.put(
            _v2_key(f"dev-{n:02d}"),
            [_v2(f"2026-11-16T10:00:{n:02d}Z", f"line {n}")],
            modified=_at("2026-11-16T10:01:00"),
        )
    ticks = iter(range(1000))
    archive = LogArchive(store, BASE, clock=lambda: float(next(ticks)))

    with pytest.raises(ArchiveDeadline):
        archive.read_days("olf-dev", [DAY], since=_at("2026-11-16T10:00:00"), deadline=5.0)

    assert len(store.opened) <= 3
