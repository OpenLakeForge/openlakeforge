"""The archive reader on collector (v2) objects, alone and beside the polling archiver's (v1)."""

from __future__ import annotations

import gzip
import io
import json
import logging
import tracemalloc
import weakref
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
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

    def list_objects_v2(  # noqa: N803 - boto3's names
        self, *, Bucket: str, Prefix: str, Delimiter: str = "", ContinuationToken: str = ""
    ) -> dict[str, Any]:
        self.listed.append(Prefix)
        under = [key for key in sorted(self.objects) if key.startswith(Prefix)]
        nested = {key for key in under if Delimiter and Delimiter in key.removeprefix(Prefix)}
        return {
            "Contents": [
                {"Key": key, **({"LastModified": self.modified[key]} if key in self.modified else {})}
                for key in under
                if key not in nested
            ],
            "CommonPrefixes": [
                {"Prefix": prefix}
                for prefix in sorted(
                    {Prefix + key.removeprefix(Prefix).split(Delimiter)[0] + Delimiter for key in nested}
                )
            ],
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


def test_a_coloured_line_is_returned_selected_and_matched_across_layouts_as_plain_text(store: _Store) -> None:
    coloured = (
        "\x1b[32m2026-11-16 09:00:00 +0000\x1b[0m - dagster.daemon.SensorDaemon - \x1b[31mERROR\x1b[0m - tick failed"
    )
    plain = "2026-11-16 09:00:00 +0000 - dagster.daemon.SensorDaemon - ERROR - tick failed"
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:00Z", coloured, pod="run-1")])
    # The archiver's copy of the same line, and one only it holds.
    store.put(
        _v1_key(),
        [_v1(f"2026-11-16T09:00:00Z {coloured}"), _v1("2026-11-16T09:00:01Z \x1b[31mERROR\x1b[0m only v1")],
    )

    assert _read(store, severity="error") == [plain, "2026-11-16T09:00:01Z ERROR only v1"]
    assert _read(store, text="- ERROR - tick") == [plain]


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
        # The Dagster daemon colours its time and level.
        (
            "\x1b[32m2026-11-24 18:02:03 +0000\x1b[0m - dagster.daemon.SensorDaemon - \x1b[34mINFO\x1b[0m - "
            "Checking for new runs",
            "info",
        ),
        (
            "\x1b[32m2026-11-24 18:02:03 +0000\x1b[0m - dagster.daemon.SchedulerDaemon - \x1b[1m\x1b[31mERROR\x1b[0m - "
            "Scheduler failed",
            "error",
        ),
        ("\x1b[33mWARNING\x1b[0m disk", "warning"),
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


# Hour partitions (#1326): the collector keys an object by the date and hour of its records.
TEMPLATE = Path(__file__).resolve().parents[4] / (
    "infra/terraform/modules/observability/log-collector/templates/fluent-bit.conf.tftpl"
)
DAY_PREFIX = "activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/"


def _hour_key(name: str, hour: int, day: str = "2026-11-16") -> str:
    return f"activations/dev/logs/v2/namespace=olf-dev/date={day}/hour={hour:02d}/{name}.ndjson.gz"


def _v2_listed(store: _Store) -> list[str]:
    return [prefix for prefix in store.listed if "/v2/" in prefix]


def test_the_collector_key_for_a_record_tag_is_the_partition_the_reader_lists(store: _Store) -> None:
    # What the S3 plugin makes of the key format for the tag `olf-dev.2026-11-16.09` and $UUID `4f1b6c2e`.
    template = next(line.split()[1] for line in TEMPLATE.read_text().splitlines() if "s3_key_format" in line)
    key = (
        template.replace("${route.key_prefix}", "activations/dev/logs/v2/")
        .replace("${stage}", "dev")
        .replace("$TAG[0]", "olf-dev")
        .replace("$TAG[1]", "2026-11-16")
        .replace("$TAG[2]", "09")
        .replace("$UUID", "4f1b6c2e")
    )
    assert "%" not in template
    assert key == "/activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/hour=09/dev-4f1b6c2e.ndjson.gz"

    store.put(key.removeprefix("/"), [_v2("2026-11-16T09:30:00Z", "keyed by its hour")])

    assert _read(store, since=_at("2026-11-16T09:00:00"), until=_at("2026-11-16T10:00:00")) == ["keyed by its hour"]


def test_a_window_lists_only_the_hours_it_touches_however_much_the_others_hold(store: _Store) -> None:
    # The incident of #1326: an outage drill flushed hours of spooled lines, all uploaded during a short run.
    flushed = _at("2026-11-16T10:01:00")
    for hour in (h for h in range(24) if h != 10):
        for n in range(50):
            store.put(_hour_key(f"dev-{n:02d}", hour), [_v2(f"2026-11-16T{hour:02d}:{n:02d}:00Z", "other")], flushed)
    store.put(_hour_key("dev-run", 10), [_v2("2026-11-16T10:02:00Z", "the run", run_id="r1")], flushed)

    found = LogArchive(store, BASE).read_days(
        "olf-dev", [DAY], run_id="r1", since=_at("2026-11-16T10:00:00"), until=_at("2026-11-16T11:00:00")
    )

    assert [line.line for line in found.lines] == ["the run"]
    # One delimiter listing finds the hours; only hour 10 is listed, not even the hour before the window.
    assert _v2_listed(store) == [DAY_PREFIX, f"{DAY_PREFIX}hour=10/"]
    assert store.opened == [_hour_key("dev-run", 10)]


def test_old_lines_uploaded_late_are_read_from_their_own_hour(store: _Store) -> None:
    store.put(
        _hour_key("dev-spooled", 3), [_v2("2026-11-16T03:15:00Z", "written at three")], _at("2026-11-16T10:00:00")
    )
    archive = LogArchive(store, BASE)

    early = archive.read_days("olf-dev", [DAY], since=_at("2026-11-16T03:10:00"), until=_at("2026-11-16T03:20:00"))
    late = archive.read_days("olf-dev", [DAY], since=_at("2026-11-16T10:00:00"), until=_at("2026-11-16T10:05:00"))

    assert [line.line for line in early.lines] == ["written at three"]
    assert late.lines == []
    assert store.opened == [_hour_key("dev-spooled", 3)]


def test_a_window_across_midnight_lists_the_last_hour_of_one_day_and_the_first_of_the_next(store: _Store) -> None:
    store.put(_hour_key("dev-a", 22), [_v2("2026-11-16T22:59:00Z", "too early")])
    store.put(_hour_key("dev-b", 23), [_v2("2026-11-16T23:58:30Z", "before midnight")])
    store.put(_hour_key("dev-c", 0, day="2026-11-17"), [_v2("2026-11-17T00:01:00Z", "after midnight")])
    store.put(_hour_key("dev-d", 1, day="2026-11-17"), [_v2("2026-11-17T01:00:00Z", "too late")])

    found = LogArchive(store, BASE).read_days(
        "olf-dev", [DAY, date(2026, 11, 17)], since=_at("2026-11-16T23:30:00"), until=_at("2026-11-17T00:30:00")
    )

    assert [line.line for line in found.lines] == ["before midnight", "after midnight"]
    next_day = DAY_PREFIX.replace("2026-11-16", "2026-11-17")
    assert _v2_listed(store) == [DAY_PREFIX, f"{DAY_PREFIX}hour=23/", next_day, f"{next_day}hour=00/"]


def test_a_window_that_misses_a_day_lists_nothing_of_it(store: _Store) -> None:
    LogArchive(store, BASE).read_days(
        "olf-dev", [DAY, date(2026, 11, 17)], since=_at("2026-11-16T09:00:00"), until=_at("2026-11-16T10:00:00")
    )

    assert _v2_listed(store) == [DAY_PREFIX]


def test_a_day_written_before_hours_and_after_reads_both_with_the_upload_skip(store: _Store) -> None:
    since = _at("2026-11-16T10:00:00")
    store.put(_v2_key("dev-flat-old"), [_v2("2026-11-16T08:00:00Z", "flat, early")], _at("2026-11-16T08:05:00"))
    store.put(_v2_key("dev-flat"), [_v2("2026-11-16T10:01:00Z", "flat, in the window")], _at("2026-11-16T10:06:00"))
    store.put(_hour_key("dev-hourly", 10), [_v2("2026-11-16T10:30:00Z", "hourly")], _at("2026-11-16T10:35:00"))
    store.put(_hour_key("dev-other", 12), [_v2("2026-11-16T12:00:00Z", "another hour")], _at("2026-11-16T12:05:00"))

    found = LogArchive(store, BASE).read_days("olf-dev", [DAY], since=since, until=_at("2026-11-16T11:00:00"))

    assert [line.line for line in found.lines] == ["flat, in the window", "hourly"]
    assert sorted(store.opened) == sorted([_v2_key("dev-flat"), _hour_key("dev-hourly", 10)])


def test_an_hour_reads_its_partition_and_the_matching_lines_of_a_flat_day(store: _Store) -> None:
    store.put(_v2_key("dev-flat"), [_v2("2026-11-16T09:59:00Z", "flat nine"), _v2("2026-11-16T10:00:00Z", "flat ten")])
    store.put(_hour_key("dev-ten", 10), [_v2("2026-11-16T10:30:00Z", "hourly ten")])
    store.put(_hour_key("dev-nine", 9), [_v2("2026-11-16T09:30:00Z", "hourly nine")])

    assert _read(store, hour=10) == ["flat ten", "hourly ten"]
    assert _v2_listed(store) == [DAY_PREFIX, f"{DAY_PREFIX}hour=10/"]


def test_a_sub_prefix_that_is_not_an_hour_is_read_whole(store: _Store) -> None:
    # A collector upgraded with chunks of the date-only tag on disk fills hour=$TAG[2] with the whole tag.
    upgraded = f"{DAY_PREFIX}hour=olf-dev.2026-11-16[2]/dev-4f1b6c2e.ndjson.gz"
    store.put(upgraded, [_v2("2026-11-16T10:02:00Z", "from before the upgrade")], _at("2026-11-16T10:04:00"))

    assert _read(store, since=_at("2026-11-16T10:00:00"), until=_at("2026-11-16T10:05:00")) == [
        "from before the upgrade"
    ]
    assert store.opened == [upgraded]


def test_paging_across_hour_partitions_dedups_replays_and_overlap_and_misses_no_line(store: _Store) -> None:
    replayed = _v2("2026-11-16T09:59:59Z", "replayed")
    store.put(_hour_key("dev-a", 9), [_v2("2026-11-16T09:40:00Z", "nine"), replayed])
    store.put(_hour_key("dev-b", 9), [replayed])
    store.put(_hour_key("dev-c", 10), [_v2("2026-11-16T10:00:00Z", "ten", pod="run-1")])
    store.put(_v1_key("10"), [_v1("2026-11-16T10:00:00Z ten"), _v1("2026-11-16T10:10:00Z v1 only")])
    archive = LogArchive(store, BASE)

    seen: list[str] = []
    after = None
    for _ in range(10):
        page = archive.read_days(
            "olf-dev", [DAY], since=_at("2026-11-16T09:30:00"), until=_at("2026-11-16T10:30:00"), limit=1, after=after
        )
        if not page.lines:
            break
        seen += [line.line or "" for line in page.lines]
        after = page.last

    assert seen == ["nine", "replayed", "ten", "2026-11-16T10:10:00Z v1 only"]


# Text search (#1347): a filter like the others, read from the message.
def test_text_keeps_the_lines_whose_message_holds_it_ignoring_case_and_combines_with_severity(store: _Store) -> None:
    store.put(
        _v2_key("dev-a"),
        [
            _v2("2026-11-16T09:00:00Z", "INFO Contract loaded"),
            _v2("2026-11-16T09:01:00Z", "ERROR contract VIOLATED on orders"),
            _v2("2026-11-16T09:02:00Z", "ERROR disk full"),
            _v2("2026-11-16T09:03:00Z", "a line naming .* literally"),
        ],
    )

    assert _read(store, text="CONTRACT") == ["INFO Contract loaded", "ERROR contract VIOLATED on orders"]
    assert _read(store, text="contract violated", severity="error") == ["ERROR contract VIOLATED on orders"]
    assert _read(store, text="contract", limit=1) == ["INFO Contract loaded"]
    # Plain text, not a pattern.
    assert _read(store, text=".*") == ["a line naming .* literally"]
    assert _read(store, text="nowhere") == []


def test_text_reads_a_v1_message_without_its_kubelet_time_and_still_answers_a_shared_line_once(store: _Store) -> None:
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:01Z", "seen twice", pod="run-1")])
    store.put(
        _v1_key(),
        [
            _v1("2026-11-16T09:00:01Z seen twice"),
            _v1("2026-11-16T09:30:00Z v1 only, seen once"),
            {**_v1(""), "line": None, "archive_error": "container not found: seen"},
        ],
    )

    lines = LogArchive(store, BASE).read("olf-dev", DAY, text="seen")

    assert [(line.source, line.line) for line in lines] == [
        ("v2", "seen twice"),
        ("v1", "2026-11-16T09:30:00Z v1 only, seen once"),
    ]
    assert _read(store, text="2026-11-16T09") == []


def test_paging_a_sparse_search_returns_every_match_once_and_only_matches(store: _Store) -> None:
    # Matches are few and far apart, between replays, ties and a v1 copy, across two hours and two objects.
    tie = "2026-11-16T09:30:00Z"
    replayed = _v2(tie, "needle replayed")
    store.put(
        _hour_key("dev-a", 9),
        [_v2(f"2026-11-16T09:{n:02d}:00Z", f"hay {n}") for n in range(0, 30)] + [replayed, _v2(tie, "NEEDLE tie")],
    )
    store.put(
        _hour_key("dev-b", 9), [replayed, *(_v2(f"2026-11-16T09:{n:02d}:00Z", f"hay {n}") for n in range(31, 60))]
    )
    store.put(_hour_key("dev-c", 10), [_v2("2026-11-16T10:00:00Z", "a needle at ten", pod="run-1")])
    store.put(_v1_key("10"), [_v1("2026-11-16T10:00:00Z a needle at ten"), _v1("2026-11-16T10:20:00Z v1 needle")])
    archive = LogArchive(store, BASE)

    seen: list[str] = []
    after = None
    for _ in range(10):
        page = archive.read_days("olf-dev", [DAY], text="needle", limit=1, after=after)
        if not page.lines:
            break
        seen += [line.line or "" for line in page.lines]
        after = page.last

    assert (sorted(seen[:2]), seen[2:]) == (
        ["NEEDLE tie", "needle replayed"],
        ["a needle at ten", "2026-11-16T10:20:00Z v1 needle"],
    )


def test_the_deadline_still_stops_a_search_that_matches_nothing(store: _Store) -> None:
    for n in range(20):
        store.put(_v2_key(f"dev-{n:02d}"), [_v2(f"2026-11-16T09:00:{n:02d}Z", f"line {n}")])
    ticks = iter(range(1000))
    archive = LogArchive(store, BASE, clock=lambda: float(next(ticks)))

    with pytest.raises(ArchiveDeadline):
        archive.read("olf-dev", DAY, text="absent", deadline=5.0)

    assert len(store.opened) <= 3


def test_the_search_text_is_not_logged(store: _Store, caplog: pytest.LogCaptureFixture) -> None:
    store.put(_v2_key("dev-a"), [_v2("2026-11-16T09:00:00Z", "customer 4711 failed")])

    with caplog.at_level(logging.DEBUG, logger="openlakeforge_services.logs"):
        assert _read(store, text="customer 4711") == ["customer 4711 failed"]

    assert caplog.records
    assert "4711" not in caplog.text


def test_a_message_holding_a_unicode_line_separator_is_one_record(store: _Store) -> None:
    # JSON leaves U+2028 and U+0085 unescaped inside a string; only `\n` ends an NDJSON record.
    records = [_v2("2026-11-16T09:00:00Z", "before after\x85end"), _v2("2026-11-16T09:00:01Z", "next")]
    body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    store.objects[_v2_key("dev-a")] = gzip.compress(body.encode())

    assert _read(store) == ["before after\x85end", "next"]


def test_an_object_is_read_without_holding_its_decompressed_lines_at_once(store: _Store) -> None:
    # An hour after a burst of logging holds objects of the collector's 8 MB `total_file_size`; a read holding one
    # whole, as bytes, text and lines, took several times that, and two reads at once ran the API out of memory.
    lines = 12_000
    filler = "0123456789abcdef" * 40
    store.put(
        _v2_key("dev-burst"),
        [
            _v2(f"2026-11-16T09:{n // 600:02d}:{n % 60:02d}Z", f"burst {n} {filler}", run_id="other")
            for n in range(lines)
        ]
        + [_v2("2026-11-16T09:30:00Z", "ERROR the run's own line", run_id="r1")],
    )
    uncompressed = len(gzip.decompress(store.objects[_v2_key("dev-burst")]))
    assert uncompressed > 8_000_000

    tracemalloc.start()
    try:
        found = _read(store, run_id="r1", severity="error")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert found == ["ERROR the run's own line"]
    assert peak < uncompressed / 8
