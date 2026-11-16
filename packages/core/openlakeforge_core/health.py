"""Provider-neutral health signals: the vocabulary and the pure rules that classify observations.

Consumers read Dagster, Floe, dbt or the object store themselves and pass what they saw in; nothing
here performs I/O, so the same observations and `now` always give the same signals.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .quality import QualityOutcome


class HealthState(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


# `unknown` outranks `healthy` so a missing observation can never be hidden by a green neighbour.
# `disabled` ranks lowest: an intentionally stopped pipeline must not turn its product red or amber.
_SEVERITY = {
    HealthState.DISABLED: 0,
    HealthState.HEALTHY: 1,
    HealthState.UNKNOWN: 2,
    HealthState.WARNING: 3,
    HealthState.CRITICAL: 4,
}


class SignalKind(StrEnum):
    EXECUTION = "execution"
    LATEST_SUCCESS = "latest_success"
    EXPECTED_RUN = "expected_run"
    FRESHNESS = "freshness"
    QUALITY = "quality"
    SERVICE = "service"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ScheduleState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    ABSENT = "absent"


@dataclass(frozen=True)
class Scope:
    """What a signal is about. `stage` is mandatory so DEV and PROD can never be confused."""

    stage: str
    domain: str | None = None
    product: str | None = None
    table: str | None = None

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("a health scope needs a stage")


@dataclass(frozen=True)
class Signal:
    """One classified fact. `evidence_uri` is an opaque pointer, never log text."""

    kind: SignalKind
    state: HealthState
    stage: str
    reason: str
    domain: str | None = None
    product: str | None = None
    table: str | None = None
    run: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    evidence_uri: str | None = None
    # Which service a SERVICE signal is about ("dagster", "trino", "object-store"); None for pipeline signals.
    component: str | None = None

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("a health signal needs a stage")
        _require_aware(self.observed_at)
        _require_aware(self.expires_at)


@dataclass(frozen=True)
class RunObservation:
    status: RunStatus
    observed_at: datetime
    run: str | None = None
    evidence_uri: str | None = None


@dataclass(frozen=True)
class ScheduleObservation:
    state: ScheduleState
    observed_at: datetime
    # When the most recent run should have started, from the schedule input (#1058); None before the first tick.
    last_expected_at: datetime | None = None
    # Start of the latest run actually seen, whatever its outcome.
    last_run_started_at: datetime | None = None
    evidence_uri: str | None = None


@dataclass(frozen=True)
class QualityObservation:
    outcome: QualityOutcome
    observed_at: datetime
    # An ERROR that does not block the pipeline is reported as a warning.
    blocking: bool = True
    run: str | None = None
    evidence_uri: str | None = None


@dataclass(frozen=True)
class WorkloadObservation:
    """What the cluster reports for one component's controllers, summed across them."""

    desired: int
    ready: int
    observed_at: datetime
    # Pods that are Pending because no node fits them, and pods waiting in CrashLoopBackOff.
    unschedulable: int = 0
    crash_looping: int = 0
    # The highest count, on any pod, of container restarts inside a recent window. The observer counts only
    # restarts whose lastState.terminated.finishedAt is within that window: the cluster's restartCount never
    # resets, so a lifetime count would keep the workload on WARNING forever.
    recent_restarts: int = 0
    evidence_uri: str | None = None


_Observation = RunObservation | ScheduleObservation | QualityObservation | WorkloadObservation


def evaluate_execution(scope: Scope, run: RunObservation | None, *, now: datetime, max_age: timedelta) -> Signal:
    """Outcome of the latest run. A run still in progress has not failed, so it is healthy."""
    _check_clock(now, max_age)
    if run is None:
        return _missing(SignalKind.EXECUTION, scope, max_age)
    if _is_stale(run, now, max_age):
        return _stale(SignalKind.EXECUTION, scope, run, max_age)
    state, reason = {
        RunStatus.SUCCEEDED: (HealthState.HEALTHY, "latest run succeeded"),
        RunStatus.RUNNING: (HealthState.HEALTHY, "latest run in progress"),
        RunStatus.FAILED: (HealthState.CRITICAL, "latest run failed"),
        RunStatus.CANCELED: (HealthState.WARNING, "latest run was canceled"),
    }[run.status]
    return _signal(SignalKind.EXECUTION, scope, state, reason, run, max_age, run=run.run)


def evaluate_latest_success(
    scope: Scope, last_success: RunObservation | None, *, now: datetime, max_age: timedelta
) -> Signal:
    """Whether a successful run is on record; a pipeline that never succeeded is unknown, not failed."""
    _check_clock(now, max_age)
    if last_success is None:
        return _missing(SignalKind.LATEST_SUCCESS, scope, max_age)
    if _is_stale(last_success, now, max_age):
        return _stale(SignalKind.LATEST_SUCCESS, scope, last_success, max_age)
    if last_success.status is not RunStatus.SUCCEEDED:
        raise ValueError("latest_success takes the latest successful run, not a run that did not succeed")
    return _signal(
        SignalKind.LATEST_SUCCESS,
        scope,
        HealthState.HEALTHY,
        "a successful run is on record",
        last_success,
        max_age,
        run=last_success.run,
    )


def evaluate_expected_run(
    scope: Scope, schedule: ScheduleObservation | None, *, now: datetime, max_age: timedelta, grace: timedelta
) -> Signal:
    """A due run that never started is critical on its own: a scheduler that is down emits no failure event."""
    _check_clock(now, max_age)
    if schedule is None:
        return _missing(SignalKind.EXPECTED_RUN, scope, max_age)
    if _is_stale(schedule, now, max_age):
        return _stale(SignalKind.EXPECTED_RUN, scope, schedule, max_age)
    if schedule.state is not ScheduleState.RUNNING:
        state, reason = HealthState.DISABLED, f"schedule is {schedule.state.value}"
    elif schedule.last_expected_at is None:
        state, reason = HealthState.HEALTHY, "no run is due yet"
    elif schedule.last_run_started_at is not None and schedule.last_run_started_at >= schedule.last_expected_at:
        state, reason = HealthState.HEALTHY, "the expected run started"
    elif now < schedule.last_expected_at + grace:
        state, reason = HealthState.HEALTHY, "the expected run is within its grace period"
    else:
        state, reason = HealthState.CRITICAL, "an expected run did not start"
    return _signal(SignalKind.EXPECTED_RUN, scope, state, reason, schedule, max_age)


def evaluate_freshness(
    scope: Scope,
    last_success: RunObservation | None,
    previous_due: datetime | None,
    *,
    now: datetime,
    grace: timedelta,
    schedule: ScheduleState,
) -> Signal:
    """Whether the pipeline kept its cadence: a success since `previous_due`, or `previous_due` is still in grace.

    `previous_due` is the latest due time whose grace the caller considers elapsed, so a due time that is still
    inside its grace must not be passed or an earlier missed run would be hidden. No bound is invented here: a
    pipeline that is manual, stopped or absent has no cadence to keep and is `disabled`, never stale. A missing
    success is `unknown` rather than stale because the caller reads a window of runs, so it cannot tell "never" from
    "older than the window".
    """
    _require_aware(now)
    if grace < timedelta(0):
        raise ValueError("grace must not be negative")
    if schedule is not ScheduleState.RUNNING:
        return _signal(SignalKind.FRESHNESS, scope, HealthState.DISABLED, f"schedule is {schedule.value}", None, grace)
    if previous_due is None:
        return _signal(
            SignalKind.FRESHNESS, scope, HealthState.UNKNOWN, "no due time could be derived from the cron", None, grace
        )
    _require_aware(previous_due)
    if last_success is None:
        return _signal(SignalKind.FRESHNESS, scope, HealthState.UNKNOWN, "no successful run on record", None, grace)
    _require_aware(last_success.observed_at)
    if last_success.status is not RunStatus.SUCCEEDED:
        raise ValueError("freshness takes the latest successful run, not a run that did not succeed")
    if last_success.observed_at >= previous_due:
        state, reason = HealthState.HEALTHY, "a run succeeded since the previous due time"
    elif now < previous_due + grace:
        state, reason = HealthState.HEALTHY, "the previous due run is within its grace period"
    else:
        state, reason = HealthState.WARNING, "no run succeeded since the previous due time plus its grace"
    return _signal(SignalKind.FRESHNESS, scope, state, reason, last_success, grace, run=last_success.run, expires=False)


def evaluate_quality(scope: Scope, quality: QualityObservation | None, *, now: datetime, max_age: timedelta) -> Signal:
    _check_clock(now, max_age)
    if quality is None:
        return _missing(SignalKind.QUALITY, scope, max_age)
    if _is_stale(quality, now, max_age):
        return _stale(SignalKind.QUALITY, scope, quality, max_age)
    if quality.outcome in (QualityOutcome.NOT_RUN, QualityOutcome.UNKNOWN):
        # A check that did not run says nothing about the data, so it must not read as a pass.
        state, reason = HealthState.UNKNOWN, "quality checks did not report an outcome"
    elif quality.outcome is QualityOutcome.PASSED:
        state, reason = HealthState.HEALTHY, "quality checks passed"
    elif quality.outcome is QualityOutcome.WARN:
        state, reason = HealthState.WARNING, "quality checks warned"
    elif quality.blocking:
        state, reason = HealthState.CRITICAL, "a blocking quality check failed"
    else:
        state, reason = HealthState.WARNING, "a non-blocking quality check failed"
    return _signal(SignalKind.QUALITY, scope, state, reason, quality, max_age, run=quality.run)


def evaluate_workload(
    scope: Scope,
    component: str,
    workload: WorkloadObservation | None,
    *,
    now: datetime,
    max_age: timedelta,
    restart_warning: int = 3,
) -> Signal:
    """Controller readiness. This proves pods exist and are ready, not that the service answers its clients."""
    _check_clock(now, max_age)
    if workload is None:
        return _service(scope, component, HealthState.UNKNOWN, "no observation", None, None, max_age)
    if _is_stale(workload, now, max_age):
        return _service(
            scope,
            component,
            HealthState.UNKNOWN,
            "observation is stale",
            workload.observed_at,
            workload.evidence_uri,
            max_age,
        )
    ready = f"{workload.ready}/{workload.desired} pods ready"
    if workload.desired == 0:
        state, reason = HealthState.WARNING, "scaled to zero"
    elif workload.unschedulable:
        state, reason = HealthState.CRITICAL, f"{ready}; {workload.unschedulable} unschedulable"
    elif workload.ready == 0:
        state, reason = HealthState.CRITICAL, ready
    elif workload.crash_looping:
        state, reason = HealthState.WARNING, f"{ready}; {workload.crash_looping} crash looping"
    elif workload.ready < workload.desired:
        state, reason = HealthState.WARNING, ready
    elif workload.recent_restarts >= restart_warning:
        state, reason = HealthState.WARNING, f"{ready}; {workload.recent_restarts} recent restarts"
    else:
        state, reason = HealthState.HEALTHY, ready
    return _service(scope, component, state, reason, workload.observed_at, workload.evidence_uri, max_age)


def service_signal(
    scope: Scope,
    component: str,
    state: HealthState,
    reason: str,
    *,
    now: datetime,
    max_age: timedelta,
    evidence_uri: str | None = None,
) -> Signal:
    """A SERVICE signal observed `now`: UNKNOWN when a probe could not answer, DISABLED for a component that is off."""
    _check_clock(now, max_age)
    return _service(scope, component, state, reason, now, evidence_uri, max_age)


def worst_state(states: Iterable[HealthState]) -> HealthState:
    """The most severe state; an empty input is `unknown`, because nothing was observed."""
    return max(states, key=_SEVERITY.__getitem__, default=HealthState.UNKNOWN)


def rollup(signals: Iterable[Signal]) -> HealthState:
    """Worst state of signals from one stage; raises on a mix so DEV can never colour PROD."""
    collected = tuple(signals)
    if len({signal.stage for signal in collected}) > 1:
        raise ValueError("signals from different stages cannot be rolled up together; use rollup_by_stage")
    return worst_state(signal.state for signal in collected)


def rollup_by_stage(signals: Iterable[Signal]) -> dict[str, HealthState]:
    by_stage: dict[str, list[Signal]] = {}
    for signal in signals:
        by_stage.setdefault(signal.stage, []).append(signal)
    return {stage: rollup(group) for stage, group in by_stage.items()}


def _require_aware(moment: datetime | None) -> None:
    # Comparing naive and aware datetimes raises deep inside the rules; fail at the boundary instead.
    if moment is not None and moment.tzinfo is None:
        raise ValueError("health timestamps must be timezone-aware")


def _missing(kind: SignalKind, scope: Scope, max_age: timedelta) -> Signal:
    return _signal(kind, scope, HealthState.UNKNOWN, "no observation", None, max_age)


def _check_clock(now: datetime, max_age: timedelta) -> None:
    _require_aware(now)
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")


def _is_stale(observation: _Observation, now: datetime, max_age: timedelta) -> bool:
    _require_aware(observation.observed_at)
    return now >= observation.observed_at + max_age


def _stale(kind: SignalKind, scope: Scope, observation: _Observation, max_age: timedelta) -> Signal:
    return _signal(kind, scope, HealthState.UNKNOWN, "observation is stale", observation, max_age)


def _service(
    scope: Scope,
    component: str,
    state: HealthState,
    reason: str,
    observed_at: datetime | None,
    evidence_uri: str | None,
    max_age: timedelta,
) -> Signal:
    return Signal(
        kind=SignalKind.SERVICE,
        state=state,
        stage=scope.stage,
        reason=reason,
        component=component,
        observed_at=observed_at,
        expires_at=observed_at + max_age if observed_at else None,
        evidence_uri=evidence_uri,
    )


def _signal(
    kind: SignalKind,
    scope: Scope,
    state: HealthState,
    reason: str,
    observation: _Observation | None,
    max_age: timedelta,
    *,
    run: str | None = None,
    expires: bool = True,
) -> Signal:
    observed_at = observation.observed_at if observation else None
    return Signal(
        kind=kind,
        state=state,
        stage=scope.stage,
        reason=reason,
        domain=scope.domain,
        product=scope.product,
        table=scope.table,
        run=run,
        observed_at=observed_at,
        # A freshness verdict expires at the next due time, which this module cannot know: the consumer re-evaluates.
        expires_at=observed_at + max_age if observed_at and expires else None,
        evidence_uri=observation.evidence_uri if observation else None,
    )
