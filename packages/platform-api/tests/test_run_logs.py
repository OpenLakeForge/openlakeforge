"""GET /api/v1/stages/{stage}/runs/{run_id}/logs: a run's window from Dagster, read across archive days."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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
    assert {prefix.rsplit("date=", 1)[1] for prefix in store.listed} == {"2026-10-28/", "2026-10-29/", "2026-10-30/"}


def test_the_line_limit_marks_the_answer_truncated(api: TestClient, mint: Mint) -> None:
    body = _get(api, mint, "?limit=1")

    assert [line["timestamp"] for line in body["lines"]] == ["2026-10-30T23:55:00Z"]
    assert body["truncated"] is True


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
