"""The archive reader on collector (v2) objects, alone and beside the polling archiver's (v1)."""

from __future__ import annotations

import gzip
import io
import json
import weakref
from datetime import UTC, date, datetime
from typing import Any

import pytest
from openlakeforge_services.logs import LogArchive, level_of

BASE = "s3://ops/activations/dev/logs"
DAY = date(2026, 11, 16)


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.listed: list[str] = []

    def put(self, key: str, records: list[dict[str, Any]]) -> None:
        self.objects[key] = gzip.compress("".join(json.dumps(record) + "\n" for record in records).encode())

    def list_objects_v2(self, *, Bucket: str, Prefix: str, ContinuationToken: str = "") -> dict[str, Any]:  # noqa: N803
        self.listed.append(Prefix)
        return {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
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

    both = archive.read_days("olf-dev", [date(2026, 11, 17), DAY])

    assert [line.line for line in both] == ["before midnight", "2026-11-17T00:00:00.05Z from v1", "after midnight"]
    assert [line.timestamp for line in both] == [
        "2026-11-16T23:59:59.9Z",
        "2026-11-17T00:00:00.05Z",
        "2026-11-17T00:00:00.1Z",
    ]
    assert [line.line for line in archive.read_days("olf-dev", [DAY, date(2026, 11, 17)], limit=1)] == [
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
