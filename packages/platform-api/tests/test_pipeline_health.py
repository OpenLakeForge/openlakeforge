"""Freshness and evidence bounds for schedules that are not daily, evaluated at a fixed `now`."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from openlakeforge_core import parse_alert_policy, resolve_conditions
from openlakeforge_services.dagster import DagsterRun, DagsterSchedule

from openlakeforge_platform_api.pipeline_health import _evaluate
from openlakeforge_platform_api.schedules import next_due

ON_SINCE = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def _utc(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    # November 2026: the 13th is a Friday, the 16th a Monday.
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


def _schedule(cron: str, *, status: str = "RUNNING", default_status: str = "STOPPED") -> DagsterSchedule:
    return DagsterSchedule(
        name="orders",
        job_name="orders_job",
        status=status,
        default_status=default_status,
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


def test_freshness_reads_the_success_that_finished_last_not_the_newest_created() -> None:
    # Dagster lists newest-created first. The 10:00 run finished at 10:10; an earlier-created run finished at 11:05,
    # after the 11:00 due time, so the pipeline is fresh at 11:16.
    newer = DagsterRun("r2", "orders_job", "SUCCESS", _utc(16, 10, 0).timestamp(), None, _utc(16, 10, 10).timestamp())
    older = DagsterRun("r1", "orders_job", "SUCCESS", _utc(16, 9, 0).timestamp(), None, _utc(16, 11, 5).timestamp())
    evaluation = _evaluate("dev", "orders_job", [newer, older], _schedule("0 * * * *"), _utc(16, 11, 16))
    states = {signal.kind.value: signal.state.value for signal in evaluation.signals}

    assert states["freshness"] == "healthy"


@pytest.mark.parametrize(
    ("status", "default_status", "expected"),
    [
        ("STOPPED", "RUNNING", "warning"),
        ("STOPPED", "STOPPED", "disabled"),
        ("RUNNING", "RUNNING", "healthy"),
        ("RUNNING", "STOPPED", "healthy"),
    ],
)
def test_expected_run_compares_the_schedule_with_its_declared_status(
    status: str, default_status: str, expected: str
) -> None:
    now = _utc(16, 9, 5)
    schedule = _schedule("0 9 * * *", status=status, default_status=default_status)
    run = DagsterRun("r1", "orders_job", "SUCCESS", _utc(16, 9).timestamp(), None, _utc(16, 9, 2).timestamp())
    evaluation = _evaluate("prod", "orders_job", [run], schedule, now)
    states = {signal.kind.value: signal.state.value for signal in evaluation.signals}

    assert states["expected_run"] == expected


def _conditions(**when: object):
    """What the winning global rule resolves to for a job, as `alerts.yml` would say it."""
    policy = parse_alert_policy(
        {
            "apiVersion": "openlakeforge.io/v1beta1",
            "kind": "AlertPolicy",
            "routes": {"ops": "ops"},
            "defaults": {"notify": ["ops"]},
            "rules": [{"match": {"domain": "*", "product": "*"}, "when": when}],
        }
    )
    return resolve_conditions(policy, "dev")


def _policy_states(
    cron: str | None, success: datetime, now: datetime, when: Mapping[str, Any] | None = None, status: str = "RUNNING"
) -> dict[str, str]:
    schedule = _schedule(cron, status=status) if cron else None
    conditions = _conditions(**when) if when else None
    evaluation = _evaluate("dev", "orders_job", [_success(success)], schedule, now, conditions=conditions)
    return {signal.kind.value: signal.state.value for signal in evaluation.signals}


def test_a_missed_runs_grace_replaces_the_default_for_that_job() -> None:
    # The 09:00 run is 20 minutes late: past the default grace, inside a declared 30 minutes.
    assert _states("0 9 * * *", _utc(15, 9, 5), _utc(16, 9, 20))["expected_run"] == "critical"
    states = _policy_states("0 9 * * *", _utc(15, 9, 5), _utc(16, 9, 20), {"missed_run": {"grace": "30m"}})

    assert states["expected_run"] == "healthy"
    assert _policy_states("0 9 * * *", _utc(15, 9, 5), _utc(16, 9, 31), {"missed_run": {"grace": "30m"}})[
        "expected_run"
    ] == ("critical")


def test_freshness_thresholds_measure_the_age_of_the_last_success() -> None:
    bounds = {"freshness": {"warn_after": "26h", "critical_after": "30h"}}
    now = _utc(16, 12)

    assert _policy_states("0 9 * * *", now - _hours(20), now, bounds)["freshness"] == "healthy"
    assert _policy_states("0 9 * * *", now - _hours(27), now, bounds)["freshness"] == "warning"
    assert _policy_states("0 9 * * *", now - _hours(31), now, bounds)["freshness"] == "critical"


@pytest.mark.parametrize("schedule", [None, "STOPPED"], ids=["no schedule", "stopped schedule"])
def test_freshness_thresholds_apply_in_any_schedule_state(schedule: str | None) -> None:
    now = _utc(16, 12)
    cron = None if schedule is None else "0 9 * * *"

    states = _policy_states(
        cron, now - _hours(31), now, {"freshness": {"critical_after": "30h"}}, schedule or "RUNNING"
    )

    assert states["freshness"] == "critical"
    # Without thresholds the same pipeline has no cadence to keep.
    assert _policy_states(cron, now - _hours(31), now, None, schedule or "RUNNING")["freshness"] == "disabled"


def test_a_freshness_grace_moves_the_run_a_cadence_owes() -> None:
    # Owed is 11:30 under a 30-minute grace, so an 11:44 success is enough; the default grace owes 11:45.
    assert _states("*/5 * * * *", _utc(16, 11, 44), _utc(16, 12, 0, 30))["freshness"] == "critical"

    assert (
        _policy_states("*/5 * * * *", _utc(16, 11, 44), _utc(16, 12, 0, 30), {"freshness": {"grace": "30m"}})[
            "freshness"
        ]
        == "healthy"
    )


def test_a_pipeline_without_a_success_stays_unknown_whatever_the_bounds() -> None:
    evaluation = _evaluate(
        "dev", "orders_job", [], None, _utc(16, 12), conditions=_conditions(freshness={"critical_after": "1h"})
    )

    assert {s.kind.value: s.state.value for s in evaluation.signals}["freshness"] == "unknown"


def _hours(count: int) -> timedelta:
    return timedelta(hours=count)


def test_a_raised_grace_reads_the_occurrence_that_opened_the_incident_as_inside_its_grace() -> None:
    """The evaluator resolves an open missed run when the signal names the same occurrence as inside its grace."""
    now = _utc(16, 9, 25)
    run = _success(_utc(15, 9, 5))
    schedule = _schedule("0 9 * * *")

    default = _evaluate("dev", "orders_job", [run], schedule, now)
    raised = _evaluate("dev", "orders_job", [run], schedule, now, conditions=_conditions(missed_run={"grace": "30m"}))
    assert (default.signals[2].state.value, raised.signals[2].state.value) == ("critical", "healthy")
    assert (raised.signals[2].reason, raised.signals[2].due) == (
        "the expected run is within its grace period",
        _utc(16, 9),
    )
