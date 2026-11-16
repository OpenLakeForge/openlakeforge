from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from openlakeforge_core import (
    HealthState,
    QualityObservation,
    QualityOutcome,
    RunObservation,
    RunStatus,
    ScheduleObservation,
    ScheduleState,
    Scope,
    Signal,
    SignalKind,
    WorkloadObservation,
    evaluate_execution,
    evaluate_expected_run,
    evaluate_latest_success,
    evaluate_quality,
    evaluate_workload,
    rollup,
    rollup_by_stage,
    service_signal,
    worst_state,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=1)
GRACE = timedelta(minutes=10)
DEV = Scope(stage="dev", domain="sales", product="orders", table="silver.orders")
FRESH = NOW - timedelta(minutes=5)
STALE = NOW - timedelta(hours=2)


def _run(status: RunStatus, observed_at: datetime = FRESH) -> RunObservation:
    return RunObservation(status, observed_at, run="run-1", evidence_uri="evidence://dev/run-1")


def _signal(state: HealthState, stage: str = "dev") -> Signal:
    return Signal(SignalKind.SERVICE, state, stage, "fixture")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.SUCCEEDED, HealthState.HEALTHY),
        (RunStatus.RUNNING, HealthState.HEALTHY),
        (RunStatus.FAILED, HealthState.CRITICAL),
        (RunStatus.CANCELED, HealthState.WARNING),
    ],
)
def test_execution_state_follows_the_latest_run(status: RunStatus, expected: HealthState) -> None:
    signal = evaluate_execution(DEV, _run(status), now=NOW, max_age=MAX_AGE)
    assert signal.state is expected
    assert (signal.stage, signal.domain, signal.product, signal.table, signal.run) == (
        "dev",
        "sales",
        "orders",
        "silver.orders",
        "run-1",
    )
    assert signal.evidence_uri == "evidence://dev/run-1"
    assert signal.expires_at == FRESH + MAX_AGE


def test_stale_observation_is_unknown_not_healthy() -> None:
    signal = evaluate_execution(DEV, _run(RunStatus.SUCCEEDED, STALE), now=NOW, max_age=MAX_AGE)
    assert signal.state is HealthState.UNKNOWN


def test_observation_expires_exactly_at_max_age() -> None:
    at_limit = _run(RunStatus.SUCCEEDED, NOW - MAX_AGE)
    assert evaluate_execution(DEV, at_limit, now=NOW, max_age=MAX_AGE).state is HealthState.UNKNOWN


def test_missing_observation_is_unknown_for_every_kind() -> None:
    signals = [
        evaluate_execution(DEV, None, now=NOW, max_age=MAX_AGE),
        evaluate_latest_success(DEV, None, now=NOW, max_age=MAX_AGE),
        evaluate_expected_run(DEV, None, now=NOW, max_age=MAX_AGE, grace=GRACE),
        evaluate_quality(DEV, None, now=NOW, max_age=MAX_AGE),
    ]
    assert [s.state for s in signals] == [HealthState.UNKNOWN] * 4
    assert [s.kind for s in signals] == [
        SignalKind.EXECUTION,
        SignalKind.LATEST_SUCCESS,
        SignalKind.EXPECTED_RUN,
        SignalKind.QUALITY,
    ]


def test_latest_success_is_healthy_when_recorded() -> None:
    signal = evaluate_latest_success(DEV, _run(RunStatus.SUCCEEDED), now=NOW, max_age=MAX_AGE)
    assert signal.state is HealthState.HEALTHY


def test_latest_success_rejects_a_run_that_did_not_succeed() -> None:
    with pytest.raises(ValueError, match="successful"):
        evaluate_latest_success(DEV, _run(RunStatus.FAILED), now=NOW, max_age=MAX_AGE)


@pytest.mark.parametrize("state", [ScheduleState.STOPPED, ScheduleState.ABSENT])
def test_stopped_or_absent_schedule_is_disabled_even_with_a_run_overdue(state: ScheduleState) -> None:
    schedule = ScheduleObservation(state, FRESH, last_expected_at=NOW - timedelta(hours=3))
    signal = evaluate_expected_run(DEV, schedule, now=NOW, max_age=MAX_AGE, grace=GRACE)
    assert signal.state is HealthState.DISABLED


def test_missed_expected_run_is_critical_without_any_failure_event() -> None:
    schedule = ScheduleObservation(ScheduleState.RUNNING, FRESH, last_expected_at=NOW - timedelta(minutes=30))
    signal = evaluate_expected_run(DEV, schedule, now=NOW, max_age=MAX_AGE, grace=GRACE)
    assert signal.state is HealthState.CRITICAL


def test_expected_run_within_grace_is_healthy() -> None:
    schedule = ScheduleObservation(ScheduleState.RUNNING, FRESH, last_expected_at=NOW - timedelta(minutes=5))
    assert evaluate_expected_run(DEV, schedule, now=NOW, max_age=MAX_AGE, grace=GRACE).state is HealthState.HEALTHY


def test_expected_run_that_started_is_healthy() -> None:
    expected = NOW - timedelta(minutes=30)
    schedule = ScheduleObservation(
        ScheduleState.RUNNING, FRESH, last_expected_at=expected, last_run_started_at=expected + timedelta(minutes=1)
    )
    assert evaluate_expected_run(DEV, schedule, now=NOW, max_age=MAX_AGE, grace=GRACE).state is HealthState.HEALTHY


def test_run_before_the_expected_time_does_not_satisfy_it() -> None:
    expected = NOW - timedelta(minutes=30)
    schedule = ScheduleObservation(
        ScheduleState.RUNNING, FRESH, last_expected_at=expected, last_run_started_at=expected - timedelta(hours=1)
    )
    assert evaluate_expected_run(DEV, schedule, now=NOW, max_age=MAX_AGE, grace=GRACE).state is HealthState.CRITICAL


def test_schedule_with_nothing_due_yet_is_healthy() -> None:
    schedule = ScheduleObservation(ScheduleState.RUNNING, FRESH)
    assert evaluate_expected_run(DEV, schedule, now=NOW, max_age=MAX_AGE, grace=GRACE).state is HealthState.HEALTHY


def test_stale_schedule_observation_is_unknown_not_critical() -> None:
    schedule = ScheduleObservation(ScheduleState.RUNNING, STALE, last_expected_at=STALE)
    assert evaluate_expected_run(DEV, schedule, now=NOW, max_age=MAX_AGE, grace=GRACE).state is HealthState.UNKNOWN


@pytest.mark.parametrize(
    ("outcome", "blocking", "expected"),
    [
        (QualityOutcome.PASS, True, HealthState.HEALTHY),
        (QualityOutcome.WARN, True, HealthState.WARNING),
        (QualityOutcome.ERROR, True, HealthState.CRITICAL),
        (QualityOutcome.ERROR, False, HealthState.WARNING),
    ],
)
def test_quality_outcome_maps_to_state(outcome: QualityOutcome, blocking: bool, expected: HealthState) -> None:
    quality = QualityObservation(outcome, FRESH, blocking=blocking, run="run-1", evidence_uri="evidence://dev/dq")
    signal = evaluate_quality(DEV, quality, now=NOW, max_age=MAX_AGE)
    assert signal.state is expected
    assert signal.kind is SignalKind.QUALITY
    assert signal.evidence_uri == "evidence://dev/dq"


def test_stale_quality_result_is_unknown() -> None:
    quality = QualityObservation(QualityOutcome.PASS, STALE)
    assert evaluate_quality(DEV, quality, now=NOW, max_age=MAX_AGE).state is HealthState.UNKNOWN


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([HealthState.HEALTHY, HealthState.UNKNOWN], HealthState.UNKNOWN),
        ([HealthState.UNKNOWN, HealthState.WARNING], HealthState.WARNING),
        ([HealthState.WARNING, HealthState.CRITICAL, HealthState.HEALTHY], HealthState.CRITICAL),
        ([HealthState.DISABLED, HealthState.HEALTHY], HealthState.HEALTHY),
        ([HealthState.DISABLED], HealthState.DISABLED),
        ([], HealthState.UNKNOWN),
    ],
)
def test_rollup_takes_the_worst_state(states: list[HealthState], expected: HealthState) -> None:
    assert rollup(_signal(state) for state in states) is expected
    assert worst_state(states) is expected


def test_rollup_refuses_to_merge_stages() -> None:
    with pytest.raises(ValueError, match="stages"):
        rollup([_signal(HealthState.HEALTHY, "dev"), _signal(HealthState.CRITICAL, "prod")])


def test_rollup_by_stage_keeps_stages_separate() -> None:
    signals = [
        _signal(HealthState.CRITICAL, "dev"),
        _signal(HealthState.HEALTHY, "prod"),
        _signal(HealthState.WARNING, "dev"),
    ]
    assert rollup_by_stage(signals) == {"dev": HealthState.CRITICAL, "prod": HealthState.HEALTHY}


def test_same_run_failing_in_dev_does_not_colour_prod() -> None:
    signals = [
        evaluate_execution(Scope(stage="dev"), _run(RunStatus.FAILED), now=NOW, max_age=MAX_AGE),
        evaluate_execution(Scope(stage="prod"), _run(RunStatus.SUCCEEDED), now=NOW, max_age=MAX_AGE),
    ]
    assert rollup_by_stage(signals) == {"dev": HealthState.CRITICAL, "prod": HealthState.HEALTHY}


def test_naive_timestamps_are_rejected() -> None:
    naive = RunObservation(RunStatus.SUCCEEDED, datetime(2026, 11, 16, 11, 55))
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_execution(DEV, naive, now=NOW, max_age=MAX_AGE)


def test_non_positive_max_age_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_age"):
        evaluate_execution(DEV, None, now=NOW, max_age=timedelta(0))


def test_scope_and_signal_need_a_stage() -> None:
    with pytest.raises(ValueError, match="stage"):
        Scope(stage="")
    with pytest.raises(ValueError, match="stage"):
        Signal(SignalKind.SERVICE, HealthState.HEALTHY, "", "fixture")


def _workload(desired: int, ready: int, observed_at: datetime = FRESH, **kwargs: int) -> WorkloadObservation:
    return WorkloadObservation(desired, ready, observed_at, **kwargs)


@pytest.mark.parametrize(
    ("workload", "expected"),
    [
        (_workload(2, 2), HealthState.HEALTHY),
        (_workload(2, 1), HealthState.WARNING),
        (_workload(2, 0), HealthState.CRITICAL),
        (_workload(1, 1, unschedulable=1), HealthState.CRITICAL),
        (_workload(1, 1, crash_looping=1), HealthState.WARNING),
        (_workload(1, 1, recent_restarts=3), HealthState.WARNING),
        (_workload(0, 0), HealthState.WARNING),
        (_workload(1, 1, STALE), HealthState.UNKNOWN),
        (None, HealthState.UNKNOWN),
    ],
)
def test_workload_state_follows_readiness_restarts_and_age(
    workload: WorkloadObservation | None, expected: HealthState
) -> None:
    signal = evaluate_workload(DEV, "dagster", workload, now=NOW, max_age=MAX_AGE)
    assert signal.state is expected
    assert signal.kind is SignalKind.SERVICE
    assert signal.component == "dagster"


def test_service_signal_carries_unknown_and_disabled_states() -> None:
    unknown = service_signal(DEV, "trino", HealthState.UNKNOWN, "probe timed out", now=NOW, max_age=MAX_AGE)
    disabled = service_signal(DEV, "trino", HealthState.DISABLED, "not deployed", now=NOW, max_age=MAX_AGE)
    assert (unknown.state, disabled.state) == (HealthState.UNKNOWN, HealthState.DISABLED)
    assert unknown.kind is SignalKind.SERVICE and unknown.component == "trino"
    assert unknown.reason == "probe timed out"


def test_service_signal_expires_max_age_after_observation() -> None:
    signal = service_signal(DEV, "dagster", HealthState.HEALTHY, "ok", now=NOW, max_age=MAX_AGE)
    assert signal.observed_at == NOW
    assert signal.expires_at == NOW + MAX_AGE
