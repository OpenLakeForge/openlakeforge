"""Freshness and evidence bounds for schedules that are not daily, evaluated at a fixed `now`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from openlakeforge_services.dagster import DagsterRun, DagsterSchedule

from openlakeforge_platform_api.pipeline_health import _evaluate
from openlakeforge_platform_api.schedules import next_due

ON_SINCE = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def _utc(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    # November 2026: the 13th is a Friday, the 16th a Monday.
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


def _schedule(cron: str) -> DagsterSchedule:
    return DagsterSchedule(
        name="orders",
        job_name="orders_job",
        status="RUNNING",
        cron_schedule=cron,
        timezone="UTC",
        started_at=ON_SINCE,
        last_tick_at=ON_SINCE,
    )


def _success(at: datetime) -> DagsterRun:
    created = at.timestamp() - 60
    return DagsterRun("r1", "orders_job", "SUCCESS", created, created + 1, at.timestamp())


def _states(cron: str, success: datetime, now: datetime) -> dict[str, str]:
    evaluation = _evaluate("dev", "orders_job", [_success(success)], _schedule(cron), now)
    return {signal.kind.value: signal.state.value for signal in evaluation.signals}


@pytest.mark.parametrize("cron", ["* * * * *", "*/5 * * * *"])
def test_a_schedule_more_frequent_than_the_grace_is_stale_once_its_runs_stop(cron: str) -> None:
    states = _states(cron, _utc(16, 11, 0), _utc(16, 12, 0, 30))

    assert states["freshness"] == "critical"


@pytest.mark.parametrize("cron", ["* * * * *", "*/5 * * * *"])
def test_a_schedule_more_frequent_than_the_grace_is_fresh_while_its_runs_succeed(cron: str) -> None:
    states = _states(cron, _utc(16, 11, 59, 30), _utc(16, 12, 0, 30))

    assert states["freshness"] == "healthy"


def test_a_five_minute_schedule_owes_the_run_due_a_grace_ago() -> None:
    # Owed is the 11:45 run (12:00 minus the 15-minute grace); a success at 11:44 predates it.
    assert _states("*/5 * * * *", _utc(16, 11, 44), _utc(16, 12, 0, 30))["freshness"] == "critical"
    assert _states("*/5 * * * *", _utc(16, 11, 46), _utc(16, 12, 0, 30))["freshness"] == "healthy"


@pytest.mark.parametrize("now", [_utc(14, 12), _utc(15, 12), _utc(16, 9, 14)])
def test_a_weekday_pipeline_is_healthy_over_the_weekend_after_fridays_success(now: datetime) -> None:
    states = _states("0 9 * * 1-5", _utc(13, 9, 5), now)

    assert states["freshness"] == "healthy"
    assert states["execution"] == "healthy"
    assert states["latest_success"] == "healthy"


def test_a_weekday_pipeline_is_stale_on_monday_when_mondays_run_did_not_succeed() -> None:
    states = _states("0 9 * * 1-5", _utc(13, 9, 5), _utc(16, 9, 16))

    assert states["freshness"] == "critical"


def test_a_weekday_pipeline_is_healthy_on_monday_inside_the_grace() -> None:
    assert _states("0 9 * * 1-5", _utc(13, 9, 5), _utc(16, 9, 10))["freshness"] == "healthy"


def test_a_daily_pipeline_keeps_its_cadence_bound() -> None:
    # Yesterday's 09:05 success is evidence until today's 09:00 plus the grace, and no longer.
    assert _states("0 9 * * *", _utc(15, 9, 5), _utc(16, 9, 10)) == {
        "execution": "healthy",
        "latest_success": "healthy",
        "expected_run": "healthy",
        "freshness": "healthy",
    }
    assert _states("0 9 * * *", _utc(15, 9, 5), _utc(16, 9, 21))["latest_success"] == "unknown"
    assert _states("0 9 * * *", _utc(15, 9, 5), _utc(16, 9, 21))["freshness"] == "critical"


def test_the_next_due_time_follows_the_cron_across_a_weekend() -> None:
    assert next_due("0 9 * * 1-5", "UTC", _utc(13, 9)) == _utc(16, 9)
    assert next_due("0 9 * * 1-5", "UTC", _utc(13, 8, 59, 59)) == _utc(13, 9)


def test_the_next_due_time_across_the_clocks_going_back_is_the_later_pass() -> None:
    after = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    # 01:30 New York repeats on 2026-11-01; Dagster fires its later pass, 06:30 UTC.
    assert next_due("30 1 * * *", "America/New_York", after) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_the_next_due_time_of_a_bad_cron_is_none() -> None:
    assert next_due("not a cron", "UTC", _utc(16, 12)) is None
    assert next_due("0 9 * * *", "No/Such_Zone", _utc(16, 12)) is None
