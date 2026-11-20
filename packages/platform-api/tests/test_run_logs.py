"""GET /api/v1/stages/{stage}/runs/{run_id}/logs: a run's window from Dagster, read across archive days."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import Mint
from fastapi.testclient import TestClient
from test_logs import DEV_BASE, ObjectStore, _v2
from test_reads import RUN, FakeDagster, _as, _settings

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.settings import Settings

RUN_ID = "8f1c2d4e-0b7a-4c55-9a10-2f6e7a3b9c01"
PATH = f"/api/v1/stages/dev/runs/{RUN_ID}/logs"


def _seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _run(start: str | None, end: str | None, created: str = "2026-10-30T23:49:00+00:00") -> dict[str, Any]:
    return {
        **RUN,
        "runId": RUN_ID,
        "status": "FAILURE" if end else "STARTED",
        "creationTime": _seconds(created),
        "startTime": _seconds(start) if start else None,
        "endTime": _seconds(end) if end else None,
    }


def _v2_key(day: str, name: str = "dev-a") -> str:
    return f"activations/dev/logs/v2/namespace=olf-dev/date={day}/{name}.ndjson.gz"


def _hour_key(day: str, hour: int, name: str) -> str:
    return f"activations/dev/logs/v2/namespace=olf-dev/date={day}/hour={hour:02d}/{name}.ndjson.gz"


def _v1(line: str) -> dict[str, Any]:
    return {
        "schema": "openlakeforge.k8s_log.v1",
        "archived_at": "2026-10-31T01:00:00+00:00",
        "namespace": "olf-dev",
        "pod": "dagster-run-8f1c2d4e-x7k2p",
        "container": "runner",
        "line": line,
    }


@pytest.fixture
def dagster() -> Iterator[FakeDagster]:
    fake = FakeDagster()
    # Crosses UTC midnight: ten minutes on each side.
    fake.runs = [_run("2026-10-30T23:50:00+00:00", "2026-10-31T00:10:00+00:00")]
    yield fake
    fake.close()


@pytest.fixture
def store() -> ObjectStore:
    fake = ObjectStore()
    fake.archive(
        _v2_key("2026-10-30"),
        [
            _v2("2026-10-30T23:40:00Z", "INFO before the run"),
            _v2("2026-10-30T23:55:00Z", "INFO first day", run_id=RUN_ID),
        ],
    )
    fake.archive(
        _v2_key("2026-10-31"),
        [
            _v2("2026-10-31T00:05:00Z", '{"level": "error", "msg": "second day"}', run_id=RUN_ID),
            _v2("2026-10-31T00:07:00Z", "other workload", run_id="another-run"),
            _v2("2026-10-31T00:20:00Z", "ERROR after the run", run_id=RUN_ID),
        ],
    )
    # An object the polling archiver wrote before the collector: no run id, the kubelet time opens the line.
    fake.archive(
        "activations/dev/logs/k8s/namespace=olf-dev/date=2026-10-31/hour=00/k8s-logs-20261031T010000Z.ndjson.gz",
        [_v1("2026-10-31T00:06:00.000000001Z WARNING from v1")],
    )
    fake.archive(_v2_key("2026-10-29"), [_v2("2026-10-29T23:55:00Z", "a day early")])
    return fake


@pytest.fixture
def api(settings: Settings, dagster: FakeDagster, store: ObjectStore) -> TestClient:
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
        log_archives={
            "dev": {"base_uri": DEV_BASE, "namespace": "olf-dev"},
            "prod": {"base_uri": "s3://ops/activations/prod/logs", "namespace": "olf-prod"},
        },
    )
    return TestClient(create_app(configured, object_store=store))


def _get(api: TestClient, mint: Mint, query: str = "", role: str = "data-engineer") -> dict[str, Any]:
    response = api.get(PATH + query, headers=_as(mint, role))
    assert response.status_code == 200, response.text
    return response.json()


def test_a_run_that_crosses_midnight_returns_both_days_in_one_ordering(
    api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    body = _get(api, mint)

    assert [(line["timestamp"], line["level"], line["source"]) for line in body["lines"]] == [
        ("2026-10-30T23:55:00Z", "info", "v2"),
        ("2026-10-31T00:05:00Z", "error", "v2"),
        ("2026-10-31T00:06:00.000000001Z", "warning", "v1"),
        ("2026-10-31T00:07:00Z", None, "v2"),
    ]
    assert (body["since"], body["until"], body["truncated"]) == (
        "2026-10-30T23:50:00+00:00",
        "2026-10-31T00:10:00+00:00",
        False,
    )
    assert set(store.listed) == {
        f"activations/dev/logs/{layout}/namespace=olf-dev/date={day}/"
        for layout in ("v2", "k8s")
        for day in ("2026-10-30", "2026-10-31")
    }


def test_match_run_keeps_only_the_records_tagged_with_the_run(api: TestClient, mint: Mint) -> None:
    body = _get(api, mint, "?match_run=true")

    assert [line["timestamp"] for line in body["lines"]] == ["2026-10-30T23:55:00Z", "2026-10-31T00:05:00Z"]


def test_since_and_until_narrow_the_window_and_never_widen_it(api: TestClient, mint: Mint) -> None:
    narrowed = _get(api, mint, "?since=2026-10-31T00:00:00Z&until=2026-11-02T00:00:00Z")
    past_the_end = _get(api, mint, "?since=2026-10-31T01:00:00Z")

    assert [line["timestamp"] for line in narrowed["lines"]] == [
        "2026-10-31T00:05:00Z",
        "2026-10-31T00:06:00.000000001Z",
        "2026-10-31T00:07:00Z",
    ]
    assert narrowed["until"] == "2026-10-31T00:10:00+00:00"
    assert past_the_end["lines"] == []


def test_a_run_not_yet_started_is_read_from_its_creation_and_a_running_one_to_now(
    api: TestClient, dagster: FakeDagster, mint: Mint
) -> None:
    dagster.runs = [_run(None, None, created="2026-10-31T00:04:00+00:00")]

    body = _get(api, mint)

    assert body["since"] == "2026-10-31T00:04:00+00:00"
    assert datetime.fromisoformat(body["until"]) > datetime(2026, 11, 1, tzinfo=UTC)
    assert [line["line"] for line in body["lines"]][-1] == "ERROR after the run"


def test_a_window_longer_than_the_day_cap_is_cut_and_says_so(
    api: TestClient, dagster: FakeDagster, store: ObjectStore, mint: Mint
) -> None:
    dagster.runs = [_run("2026-10-28T12:00:00+00:00", "2026-11-02T12:00:00+00:00")]

    body = _get(api, mint)

    assert body["truncated"] is True
    assert body["until"] == "2026-10-31T00:00:00+00:00"
    listed = {(prefix.split("/")[3], prefix.rsplit("date=", 1)[1]) for prefix in store.listed}
    # Three event days; v1 also lists the day after the cut, where lines of the cut's last hour were archived.
    assert listed == {("v2", day) for day in ("2026-10-28/", "2026-10-29/", "2026-10-30/")} | {
        ("k8s", day) for day in ("2026-10-28/", "2026-10-29/", "2026-10-30/", "2026-10-31/")
    }

    store.listed.clear()
    following = _get(api, mint, f"?after={body['next']}")

    assert following["since"] == "2026-10-31T00:00:00+00:00"
    assert following["lines"][0]["timestamp"] == "2026-10-31T00:05:00Z"
    assert "activations/dev/logs/v2/namespace=olf-dev/date=2026-10-30/" not in store.listed


def test_the_line_limit_marks_the_answer_truncated(api: TestClient, mint: Mint) -> None:
    body = _get(api, mint, "?limit=1")

    assert [line["timestamp"] for line in body["lines"]] == ["2026-10-30T23:55:00Z"]
    assert body["truncated"] is True
    assert body["next"]


def test_a_v1_line_archived_after_midnight_for_a_run_that_ended_before_it_is_read(
    api: TestClient, dagster: FakeDagster, store: ObjectStore, mint: Mint
) -> None:
    # The archiver ran at 00:00:05 and read the hour before it, so its object sits in the next day's partition.
    dagster.runs = [_run("2026-10-30T23:30:00+00:00", "2026-10-30T23:59:30+00:00")]
    store.objects.clear()
    store.archive(
        "activations/dev/logs/k8s/namespace=olf-dev/date=2026-10-31/hour=00/k8s-logs-20261031T000005Z.ndjson.gz",
        [
            {**_v1("2026-10-30T23:59:10Z ERROR the last line"), "archived_at": "2026-10-31T00:00:05+00:00"},
            {**_v1("2026-10-31T00:00:01Z after the run"), "archived_at": "2026-10-31T00:00:05+00:00"},
        ],
    )

    body = _get(api, mint)

    assert [(line["timestamp"], line["level"]) for line in body["lines"]] == [("2026-10-30T23:59:10Z", "error")]
    assert (body["until"], body["truncated"], body["next"]) == ("2026-10-30T23:59:30+00:00", False, None)


def test_lines_that_share_one_timestamp_are_paged_through_without_loss_or_repeat(
    api: TestClient, store: ObjectStore, mint: Mint
) -> None:
    tie = "2026-10-31T00:08:00Z"
    replayed = _v2(tie, "second")
    store.objects.clear()
    store.archive(_v2_key("2026-10-31", "dev-a"), [_v2(tie, "first"), replayed])
    # A collector replay of the second record, in another object.
    store.archive(_v2_key("2026-10-31", "dev-b"), [replayed, _v2(tie, "third")])

    seen: list[str] = []
    query = "?limit=1"
    for _ in range(5):
        body = _get(api, mint, query)
        seen += [line["line"] for line in body["lines"]]
        if not body["next"]:
            break
        query = f"?limit=1&after={body['next']}"

    assert sorted(seen) == ["first", "second", "third"]
    assert len(seen) == 3


def test_a_short_run_after_a_busy_day_opens_only_the_objects_uploaded_since_it_started(
    api: TestClient, dagster: FakeDagster, store: ObjectStore, mint: Mint
) -> None:
    # The e2e drill-down of #1326: hours of the stage's objects before a run of a few minutes.
    dagster.runs = [_run("2026-10-31T10:00:00+00:00", "2026-10-31T10:04:00+00:00")]
    store.objects.clear()
    earlier = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
    for n in range(120):
        at = earlier + timedelta(minutes=n)
        store.archive(_v2_key("2026-10-31", f"dev-{n:03d}"), [_v2(f"{at:%Y-%m-%dT%H:%M:%S}Z", "earlier")], at)
    for n in range(4):
        at = datetime(2026, 10, 31, 10, n, 30, tzinfo=UTC)
        store.archive(
            _v2_key("2026-10-31", f"run-{n}"),
            [_v2(f"{at:%Y-%m-%dT%H:%M:%S}Z", f"run line {n}", run_id=RUN_ID)],
            at + timedelta(seconds=5),
        )

    body = _get(api, mint, "?match_run=true&limit=200")

    assert [line["line"] for line in body["lines"]] == [f"run line {n}" for n in range(4)]
    # Of 124 objects listed, the run's four and the fifteen uploaded from 09:45 on: the clock slack before 10:00.
    assert len(store.opened) == 4 + 15


def test_a_short_run_after_an_outage_flush_lists_and_opens_only_its_own_hour(
    api: TestClient, dagster: FakeDagster, store: ObjectStore, mint: Mint
) -> None:
    # Run 44754438238 of #1338: the spool-limit drill flushed hours of spooled lines during a run of a few minutes, so
    # their upload time is inside the run's window and the upload skip cannot rule them out.
    dagster.runs = [_run("2026-10-31T10:00:00+00:00", "2026-10-31T10:04:00+00:00")]
    store.objects.clear()
    flushed = datetime(2026, 10, 31, 10, 1, tzinfo=UTC)
    for hour in range(10):
        for n in range(30):
            at = datetime(2026, 10, 31, hour, n, tzinfo=UTC)
            store.archive(
                _hour_key("2026-10-31", hour, f"dev-{n:02d}"), [_v2(f"{at:%Y-%m-%dT%H:%M:%S}Z", "spooled")], flushed
            )
    store.archive(
        _hour_key("2026-10-31", 10, "dev-run"),
        [_v2("2026-10-31T10:02:00Z", "run line", run_id=RUN_ID)],
        datetime(2026, 10, 31, 10, 5, tzinfo=UTC),
    )

    body = _get(api, mint, "?match_run=true&limit=200")

    assert [line["line"] for line in body["lines"]] == ["run line"]
    day = "activations/dev/logs/v2/namespace=olf-dev/date=2026-10-31/"
    assert [prefix for prefix in store.listed if "/v2/" in prefix] == [day, f"{day}hour=10/"]
    assert store.opened == [_hour_key("2026-10-31", 10, "dev-run")]


@pytest.mark.parametrize("cursor", ["nope", "W10", "WzEsMiwzXQ"])
def test_a_cursor_this_route_did_not_return_is_refused(api: TestClient, mint: Mint, cursor: str) -> None:
    assert api.get(f"{PATH}?after={cursor}", headers=_as(mint, "data-engineer")).status_code == 422


def test_an_unknown_run_is_not_found(api: TestClient, mint: Mint, store: ObjectStore) -> None:
    response = api.get("/api/v1/stages/dev/runs/nope/logs", headers=_as(mint, "data-engineer"))

    assert response.status_code == 404
    assert store.listed == []


@pytest.mark.parametrize(("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403)])
def test_the_route_admits_data_engineer_and_above(api: TestClient, mint: Mint, role: str, status: int) -> None:
    assert api.get(PATH, headers=_as(mint, role)).status_code == status


def test_a_dev_caller_cannot_read_a_prod_run_and_nothing_is_read_for_them(
    api: TestClient, dagster: FakeDagster, store: ObjectStore, mint: Mint
) -> None:
    response = api.get(f"/api/v1/stages/prod/runs/{RUN_ID}/logs", headers=_as(mint, "data-engineer"))

    assert response.status_code == 403
    assert store.listed == []
    assert dagster.variables == []


def test_a_stage_without_dagster_is_not_found(api: TestClient, mint: Mint) -> None:
    response = api.get(f"/api/v1/stages/prod/runs/{RUN_ID}/logs", headers=_as(mint, "platform-admin"))

    assert response.status_code == 404
    assert "no Dagster" in response.json()["detail"]


def test_a_sparse_search_pages_through_the_day_cap_and_returns_every_match_once(
    api: TestClient, dagster: FakeDagster, store: ObjectStore, mint: Mint
) -> None:
    # Five days of a run, one read covering three: matches on the first and the last day only, and two that tie.
    dagster.runs = [_run("2026-10-28T12:00:00+00:00", "2026-11-02T12:00:00+00:00")]
    store.objects.clear()
    for day in ("2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"):
        store.archive(_v2_key(day, "dev-hay"), [_v2(f"{day}T{h:02d}:00:00Z", f"hay {day} {h}") for h in range(13, 23)])
    store.archive(_v2_key("2026-10-28", "dev-a"), [_v2("2026-10-28T13:30:00Z", "ERROR Needle one")])
    store.archive(
        _v2_key("2026-11-01", "dev-b"),
        [_v2("2026-11-01T14:00:00Z", "WARNING needle two"), _v2("2026-11-01T14:00:00Z", "INFO needle three")],
    )

    pages: list[tuple[list[str], bool]] = []
    query = "?q=needle&limit=1"
    for _ in range(10):
        body = _get(api, mint, query)
        pages.append(([line["line"] for line in body["lines"]], body["truncated"]))
        assert body["truncated"] is (body["next"] is not None)
        if not body["next"]:
            break
        query = f"?q=needle&limit=1&after={body['next']}"

    seen = [line for lines, _ in pages for line in lines]
    assert (seen[0], sorted(seen[1:])) == ("ERROR Needle one", ["INFO needle three", "WARNING needle two"])
    # A read that reached the day cap with no match answers nothing, still truncated: the days past it are unread.
    assert ([], True) in pages
    assert pages[-1][1] is False

    floor = _get(api, mint, "?q=NEEDLE&severity=warning&limit=10")
    assert [line["line"] for line in floor["lines"]] == ["ERROR Needle one"]
    assert floor["next"]
    assert [line["line"] for line in _get(api, mint, f"?q=NEEDLE&severity=warning&after={floor['next']}")["lines"]] == [
        "WARNING needle two"
    ]


@pytest.mark.parametrize("query", ["?q=", "?q=" + "x" * 201, "?q=a%1Bb"])
def test_a_malformed_search_is_refused_before_anything_is_read(
    api: TestClient, dagster: FakeDagster, store: ObjectStore, mint: Mint, query: str
) -> None:
    assert api.get(PATH + query, headers=_as(mint, "data-engineer")).status_code == 422
    assert store.listed == []
    assert dagster.variables == []
