"""The stage and pipeline health routes against a loopback Dagster that also answers the Schedules query."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import Mint
from fastapi.testclient import TestClient
from openlakeforge_core import Signal
from test_reads import UNREACHABLE, FakeDagster, _as, _settings

from openlakeforge_platform_api.app import create_app
from openlakeforge_platform_api.service_probes import StageTargets
from openlakeforge_platform_api.settings import Settings, SettingsError

NOW = time.time()
HOUR = 3600


def _run(run_id: str, status: str, age: float, job: str = "orders_job") -> dict[str, Any]:
    created = NOW - age
    return {
        "runId": run_id,
        "jobName": job,
        "status": status,
        "creationTime": created,
        "startTime": created + 1,
        "endTime": None if status == "STARTED" else created + 60,
        "tags": [{"key": "secret/tag", "value": "do-not-expose"}],
    }


class ScheduledDagster(FakeDagster):
    def __init__(self) -> None:
        super().__init__()
        self.schedules: list[dict[str, Any]] = []

    def answer(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "query Schedules" in query:
            repository = {"schedules": self.schedules}
            return {"repositoriesOrError": {"__typename": "RepositoryConnection", "nodes": [repository]}}
        return super().answer(query, variables)


def _schedule(
    status: str,
    due_age: float,
    *,
    tick_age: float | None = None,
    started_age: float | None = None,
    declared: str = "STOPPED",
) -> dict[str, Any]:
    """A daily schedule whose latest due time was `due_age` seconds ago, whatever its ticks say."""
    due = datetime.fromtimestamp(NOW - due_age, UTC)
    ticks = [] if tick_age is None else [{"timestamp": NOW - tick_age}]
    started = None if started_age is None else NOW - started_age
    return {
        "name": "orders_daily",
        "pipelineName": "orders_job",
        "cronSchedule": f"{due.minute} {due.hour} * * *",
        "executionTimezone": "UTC",
        "defaultStatus": declared,
        "scheduleState": {
            "status": status,
            "typeSpecificData": {"__typename": "ScheduleData", "startTimestamp": started},
            "ticks": ticks,
        },
    }


def _signal_of(api: TestClient, mint: Mint, kind: str) -> dict[str, Any]:
    (pipeline,) = api.get(PIPELINES, headers=_as(mint, "data-engineer")).json()["pipelines"]
    return next(signal for signal in pipeline["signals"] if signal["kind"] == kind)


def _expected_run(api: TestClient, mint: Mint) -> dict[str, Any]:
    return _signal_of(api, mint, "expected_run")


def _freshness(api: TestClient, mint: Mint) -> dict[str, Any]:
    return _signal_of(api, mint, "freshness")


@pytest.fixture
def dagster() -> Iterator[ScheduledDagster]:
    fake = ScheduledDagster()
    yield fake
    fake.close()


@pytest.fixture
def api(settings: Settings, dagster: ScheduledDagster) -> TestClient:
    # prod is enabled but has no Dagster, as in test_reads.
    return TestClient(create_app(_settings(settings, dagster_graphql_urls={"dev": f"{dagster.url}/graphql"})))


STAGE = "/api/v1/stages/dev/health"
PIPELINES = "/api/v1/stages/dev/health/pipelines"


@pytest.mark.parametrize("path", [STAGE, PIPELINES])
@pytest.mark.parametrize(
    ("role", "status"), [("platform-admin", 200), ("data-engineer", 200), ("analyst", 403), ("viewer", 403)]
)
def test_health_admits_data_engineer_and_above(api: TestClient, mint: Mint, path: str, role: str, status: int) -> None:
    assert api.get(path, headers=_as(mint, role)).status_code == status


@pytest.mark.parametrize("path", [STAGE, PIPELINES])
def test_a_direct_request_without_a_token_is_denied(api: TestClient, path: str) -> None:
    response = api.get(path)

    assert response.status_code == 401
    assert "orders_job" not in response.text


@pytest.mark.parametrize("path", [STAGE, PIPELINES])
def test_a_stage_that_is_not_enabled_is_refused(api: TestClient, mint: Mint, path: str) -> None:
    assert api.get(path.replace("/dev/", "/staging/"), headers=_as(mint, "platform-admin")).status_code == 404


def test_a_healthy_scheduled_pipeline_reports_its_runs_and_freshness(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r2", "SUCCESS", HOUR), _run("r1", "FAILURE", 5 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, tick_age=2 * HOUR)]

    body = api.get(PIPELINES, headers=_as(mint, "data-engineer")).json()

    (pipeline,) = body["pipelines"]
    assert pipeline["state"] == "healthy"
    assert pipeline["latest_run"]["run_id"] == "r2"
    assert pipeline["last_success"]["run_id"] == "r2"
    assert pipeline["failed_runs"] == 1
    assert pipeline["schedule"]["status"] == "running"
    assert 0 < pipeline["freshness"]["age_seconds"] < 2 * HOUR
    assert "do-not-expose" not in str(body)
    assert body["thresholds"]["schedule_grace_seconds"] > 0


def test_a_failed_latest_run_is_critical_and_rolls_up_to_the_stage(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r2", "FAILURE", HOUR), _run("r1", "SUCCESS", 5 * HOUR)]

    stage = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    assert stage["state"] == "critical"
    assert stage["pipelines"] == [{"job": "orders_job", "state": "critical"}]


def test_a_stopped_scheduler_after_yesterdays_run_is_critical_once_todays_run_is_overdue(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    # The daemon stopped after yesterday's run: its last tick is yesterday's, and the webserver still answers.
    dagster.runs = [_run("r1", "SUCCESS", 22 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, tick_age=22 * HOUR)]

    assert _expected_run(api, mint)["state"] == "critical"
    stage = api.get(STAGE, headers=_as(mint, "data-engineer")).json()
    assert stage["state"] == "critical"
    assert stage["pipelines"] == [{"job": "orders_job", "state": "critical"}]


def test_a_schedule_within_its_grace_period_is_not_yet_critical(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 24 * HOUR + 60)]
    dagster.schedules = [_schedule("RUNNING", 60, tick_age=24 * HOUR)]

    assert _expected_run(api, mint)["state"] == "healthy"


def test_a_running_schedule_that_never_ticked_is_unknown_not_healthy(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    # No tick and no start time: nothing shows the schedule was on when its run came due.
    dagster.runs = [_run("r1", "SUCCESS", 30 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR)]

    assert _expected_run(api, mint)["state"] == "unknown"
    assert api.get(STAGE, headers=_as(mint, "data-engineer")).json()["state"] != "healthy"


def test_a_schedule_known_to_be_on_that_never_ticked_is_critical(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 30 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, started_age=3 * 24 * HOUR)]

    assert _expected_run(api, mint)["state"] == "critical"


def test_a_run_before_the_schedule_was_switched_on_is_not_expected(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 30 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, started_age=HOUR)]

    assert _expected_run(api, mint)["state"] == "healthy"


def test_a_run_started_after_the_latest_due_time_is_healthy(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r2", "SUCCESS", HOUR), _run("r1", "SUCCESS", 25 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, tick_age=2 * HOUR)]

    assert _expected_run(api, mint)["state"] == "healthy"


def test_a_stopped_schedule_is_disabled_whatever_its_cron_says(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 30 * HOUR)]
    dagster.schedules = [_schedule("STOPPED", 2 * HOUR)]

    assert _expected_run(api, mint)["state"] == "disabled"


def test_a_cron_that_cannot_be_read_is_unknown_not_healthy(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", HOUR)]
    dagster.schedules = [{**_schedule("RUNNING", 2 * HOUR), "cronSchedule": "['0 1 * * *', '0 2 * * *']"}]

    assert _expected_run(api, mint)["state"] == "unknown"


def test_a_daily_pipeline_with_a_success_since_the_previous_due_time_is_fresh(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, tick_age=2 * HOUR)]

    assert _freshness(api, mint)["state"] == "healthy"


def test_a_daily_pipeline_whose_latest_run_is_overdue_is_stale(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 22 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, tick_age=22 * HOUR)]

    assert _freshness(api, mint)["state"] == "critical"


def test_a_due_run_still_inside_its_grace_does_not_hide_the_one_before_it(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    # The latest due time was a minute ago (within grace), but yesterday's due run never succeeded either.
    dagster.runs = [_run("r1", "SUCCESS", 49 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 60, tick_age=49 * HOUR)]

    assert _freshness(api, mint)["state"] == "critical"


def test_a_due_run_inside_its_grace_is_not_yet_stale(api: TestClient, mint: Mint, dagster: ScheduledDagster) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 24 * HOUR + 60)]
    dagster.schedules = [_schedule("RUNNING", 60, tick_age=24 * HOUR)]

    assert _freshness(api, mint)["state"] == "healthy"


@pytest.mark.parametrize("status", ["STOPPED", None])
def test_a_stopped_or_manual_pipeline_is_disabled_never_stale(
    api: TestClient, mint: Mint, dagster: ScheduledDagster, status: str | None
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 30 * 24 * HOUR)]
    dagster.schedules = [] if status is None else [_schedule(status, 2 * HOUR)]

    assert _freshness(api, mint)["state"] == "disabled"


def test_a_schedule_not_shown_to_be_on_gives_unknown_freshness(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 30 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR)]

    assert _freshness(api, mint)["state"] == "unknown"


def test_a_success_older_than_one_cadence_stops_being_evidence(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    # Six days old is inside the old seven-day bound, but a daily pipeline's own bound is one day plus the grace.
    dagster.runs = [_run("r1", "SUCCESS", 6 * 24 * HOUR)]
    dagster.schedules = [_schedule("RUNNING", 2 * HOUR, started_age=30 * 24 * HOUR)]

    (pipeline,) = api.get(PIPELINES, headers=_as(mint, "data-engineer")).json()["pipelines"]
    states = {signal["kind"]: signal["state"] for signal in pipeline["signals"]}
    assert states["latest_success"] == "unknown"
    assert states["freshness"] == "critical"


def test_slim_with_no_metrics_still_answers_and_never_calls_absent_integrations_healthy(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = [_run("r1", "SUCCESS", HOUR)]
    dagster.schedules = [_schedule("STOPPED", 2 * HOUR)]

    stage = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    assert stage["state"] == "healthy"
    integrations = {item["name"]: item["status"] for item in stage["integrations"]}
    assert integrations == {
        "dagster": "available",
        "services": "disabled",
        "quality": "not_provided",
        "quality-sync": "not_provided",
        "metrics": "not_provided",
    }


SUMMARY = "s3://ops/activations/dev/quality-sync/summary.json"


class RecordingProber:
    def __init__(self) -> None:
        self.targets: list[StageTargets] = []

    def probe(self, targets: StageTargets) -> list[Signal]:
        self.targets.append(targets)
        return []


def test_a_governed_stage_has_its_publication_read_from_its_own_summary(
    settings: Settings, dagster: ScheduledDagster, mint: Mint
) -> None:
    prober = RecordingProber()
    configured = _settings(
        settings,
        dagster_graphql_urls={"dev": f"{dagster.url}/graphql"},
        stage_namespaces={"dev": "olf-dev"},
        quality_sync_summaries={"dev": SUMMARY},
    )
    api = TestClient(create_app(configured, service_probes=prober))

    stage = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    assert {item["name"]: item["status"] for item in stage["integrations"]}["quality-sync"] == "available"
    assert [targets.quality_sync_summary_uri for targets in prober.targets] == [SUMMARY]


@pytest.mark.parametrize(
    "summaries",
    [{"staging": SUMMARY}, {"dev": "s3://ops/activations/dev/quality-sync/"}, {"dev": "https://ops/summary.json"}],
)
def test_a_summary_setting_outside_the_stages_or_not_one_s3_object_is_refused(
    settings: Settings, summaries: dict[str, str]
) -> None:
    with pytest.raises(SettingsError, match="quality_sync_summaries"):
        _settings(settings, quality_sync_summaries=summaries)


def test_a_pipeline_that_never_ran_is_unknown_not_healthy(
    api: TestClient, mint: Mint, dagster: ScheduledDagster
) -> None:
    dagster.runs = []

    stage = api.get(STAGE, headers=_as(mint, "data-engineer")).json()

    assert stage["state"] == "unknown"


def test_a_stale_latest_run_is_unknown_not_healthy(api: TestClient, mint: Mint, dagster: ScheduledDagster) -> None:
    dagster.runs = [_run("r1", "SUCCESS", 30 * 24 * HOUR)]

    assert api.get(STAGE, headers=_as(mint, "data-engineer")).json()["state"] == "unknown"


def test_an_unreachable_dagster_is_unknown_and_unavailable_with_no_error_text(settings: Settings, mint: Mint) -> None:
    api = TestClient(create_app(_settings(settings, dagster_graphql_urls={"dev": UNREACHABLE})))

    stage = api.get(STAGE, headers=_as(mint, "data-engineer"))

    assert stage.status_code == 200
    assert stage.json()["state"] == "unknown"
    assert stage.json()["integrations"][0] == {
        "name": "dagster",
        "status": "unavailable",
        "reason": "Dagster could not be read",
    }
    assert api.get(PIPELINES, headers=_as(mint, "data-engineer")).json()["pipelines"] == []


def test_a_stage_with_no_dagster_is_disabled_for_stage_health_and_absent_for_pipelines(
    api: TestClient, mint: Mint
) -> None:
    headers = _as(mint, "data-engineer")

    stage = api.get("/api/v1/stages/prod/health", headers=headers).json()

    assert stage["state"] == "unknown"
    assert stage["integrations"][0]["status"] == "disabled"
    assert api.get("/api/v1/stages/prod/health/pipelines", headers=headers).status_code == 404


@pytest.mark.parametrize(
    ("status", "declared", "expected"),
    [("STOPPED", "RUNNING", "warning"), ("STOPPED", "STOPPED", "disabled")],
)
def test_a_stopped_schedule_is_a_mismatch_only_when_the_cadence_declares_it_running(
    api: TestClient, mint: Mint, dagster: ScheduledDagster, status: str, declared: str, expected: str
) -> None:
    dagster.schedules = [_schedule(status, 2 * HOUR, declared=declared)]

    assert _expected_run(api, mint)["state"] == expected
