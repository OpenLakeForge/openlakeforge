"""Per-stage pipeline health from Dagster reads, classified by `openlakeforge_core.health`.

Takes a Dagster client rather than a request so a caller outside the HTTP app, such as a CronJob, can reuse it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openlakeforge_core.alert_policy import (
    DEFAULT_GRACE,
    Freshness,
    MissedRun,
    Quality,
    ResolvedCondition,
    ResolvedPolicy,
)
from openlakeforge_core.health import (
    HealthState,
    RunObservation,
    RunStatus,
    ScheduleObservation,
    ScheduleState,
    Scope,
    Signal,
    evaluate_execution,
    evaluate_expected_run,
    evaluate_freshness,
    evaluate_freshness_age,
    evaluate_latest_success,
    evaluate_quality,
    rollup,
)
from openlakeforge_core.quality import QualityManifest
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterRun, DagsterSchedule

from openlakeforge_platform_api.models import IntegrationStatus
from openlakeforge_platform_api.quality_health import DOMAIN_TAG, QualityReader, quality_signal
from openlakeforge_platform_api.schedules import latest_due, next_due

log = logging.getLogger(__name__)

# Provisional (#1127), and only for a pipeline with no running schedule: a manual pipeline untouched for longer reads
# `unknown`, which is the honest answer about it. A scheduled pipeline's bound is its own cadence (`_run_max_age`).
RUN_MAX_AGE = timedelta(days=7)
# The schedule is read as it is evaluated, so its max_age only matters to the evaluator's contract.
SCHEDULE_MAX_AGE = timedelta(minutes=5)
# Dagster's scheduler ticks once a minute and a run is created after the tick; this leaves room for a slow queue.
SCHEDULE_GRACE = DEFAULT_GRACE
# Runs read per pipeline: enough to find a recent success and count failures without paging.
RUN_WINDOW = 25

RUN_STATUS = {
    "SUCCESS": RunStatus.SUCCEEDED,
    "FAILURE": RunStatus.FAILED,
    "CANCELED": RunStatus.CANCELED,
    "CANCELING": RunStatus.CANCELED,
}


@dataclass(frozen=True)
class QualityEvidence:
    """The run a product pipeline's quality signal was read from, and that run's manifest.

    The quality counts route reads the manifest from here rather than again, so its counts and the signal always
    describe the same run. `signal` is None while no run has answered (`_quality`); `manifest` is None when the
    run's manifest is absent or unusable.
    """

    signal: Signal | None
    run_id: str | None
    domain: str | None
    manifest: QualityManifest | None


@dataclass(frozen=True)
class PipelineEvaluation:
    job: str
    runs: list[DagsterRun]
    schedule: DagsterSchedule | None
    # When the schedule's cron last came due; None without a schedule or when its cron cannot be read.
    due: datetime | None
    signals: list[Signal]
    # Set for a product pipeline with a run when the stage has a quality reader; None otherwise.
    quality: QualityEvidence | None = None

    @property
    def state(self) -> HealthState:
        return rollup(self.signals)


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _observation(run: DagsterRun) -> RunObservation:
    return RunObservation(
        status=RUN_STATUS.get(run.status, RunStatus.RUNNING),
        observed_at=at(run.ended_at or run.started_at or run.created_at),
        run=run.run_id,
    )


def _expected(
    schedule: DagsterSchedule | None,
    runs: list[DagsterRun],
    due: datetime | None,
    now: datetime,
    grace: timedelta = SCHEDULE_GRACE,
) -> ScheduleObservation | None:
    """The schedule as the expected-run rule reads it; None, which reads `unknown`, when nothing can be said.

    The due time comes from the cron, never from a tick: a stopped scheduler daemon leaves no tick behind, and a
    due time taken from the last tick would stay on the last day it ran.
    """
    if schedule is None:
        return ScheduleObservation(ScheduleState.ABSENT, now)
    if schedule.status != "RUNNING":
        return ScheduleObservation(ScheduleState.STOPPED, now, declared_running=schedule.default_status == "RUNNING")
    if due is None:
        return None
    # Any run counts, a manual one included: Dagster does not say which run a tick requested.
    last_run = at(runs[0].created_at) if runs else None
    if schedule.started_at is not None:
        # An occurrence before the schedule was switched on was never expected.
        if due < at(schedule.started_at):
            return ScheduleObservation(ScheduleState.RUNNING, now, last_run_started_at=last_run)
    elif schedule.last_tick_at is None and now >= due + grace and not (last_run and last_run >= due):
        # Dagster stores no start time for a schedule that is on by default in code. With no tick either, nothing
        # shows the schedule was on at its due time (it may have been deployed after it), so a missing run is not
        # evidence: `unknown`, never `healthy`.
        return None
    return ScheduleObservation(ScheduleState.RUNNING, now, last_expected_at=due, last_run_started_at=last_run)


def _owed_due(schedule: DagsterSchedule | None, now: datetime, grace: timedelta) -> datetime | None:
    """The latest due time at or before `now - grace`, from the cron alone: the run that should have succeeded.

    Taken straight from the cron rather than stepped back from the latest due time, so a schedule that fires more
    often than the grace (every minute, every five) still owes a run: stepping back one occurrence lands inside the
    grace again. None where the schedule is not running or its cron cannot be read.
    """
    if schedule is None or schedule.status != "RUNNING":
        return None
    return latest_due(schedule.cron_schedule, schedule.timezone, now - grace)


def _evidence_bound(schedule: DagsterSchedule | None, owed: datetime | None, grace: timedelta) -> timedelta:
    """How long a run stays evidence: the gap from the owed due time to the next one, plus the grace.

    A success stays valid until the next due time after it, so a weekday cron's Friday success is still evidence on
    Saturday and Sunday. Without a running schedule, or when the cron cannot be read, the provisional seven days.
    """
    if schedule is None or owed is None:
        return RUN_MAX_AGE
    following = next_due(schedule.cron_schedule, schedule.timezone, owed)
    return following - owed + grace if following else RUN_MAX_AGE


def _owed(schedule: DagsterSchedule | None, runs: list[DagsterRun], owed: datetime | None) -> datetime | None:
    """`owed` when the schedule can be shown to have been on at that time, for the reason given in `_expected`.

    None, which reads `unknown`, otherwise.
    """
    if owed is None or schedule is None:
        return None
    if schedule.started_at is not None:
        return owed if owed >= at(schedule.started_at) else None
    if schedule.last_tick_at is None and not (runs and at(runs[0].created_at) >= owed):
        return None
    return owed


def _quality(
    quality: QualityReader,
    scope: Scope,
    job: str,
    runs: list[DagsterRun],
    now: datetime,
    max_age: timedelta,
    level: str = "warn",
) -> QualityEvidence | None:
    """The quality signal of the latest run's manifest, where a failed run's blocking errors are, or None for a job
    that is not a product pipeline.

    The manifest is published after every step and omits dbt's results until dbt's evidence is read, so one from a
    run that did not succeed (in progress, canceled, or failed before dbt reported) may lack the checks still to
    come, and a pass in it proves nothing: a retry that has cleared Silver would resolve the incident the previous
    run's dbt failure opened. Only a successful run's complete manifest can read `healthy`. Runs are walked newest
    first: one that did not succeed answers only with a warning or critical, and is otherwise passed over, so a
    failure between an old success and a Silver-only retry still speaks. The first success answers with its own
    manifest (`unknown` while that is incomplete) and ends the walk; with none reached there is no signal, which
    leaves an open incident open. A newer run whose manifest exists but cannot be read may be hiding a failure,
    so a success after it cannot answer `healthy`: it answers `unknown`, and that run is the evidence named. A run
    with no manifest hides nothing.
    """
    unreadable: DagsterRun | None = None
    for run in runs:
        read = quality_signal(quality, scope, job, run.run_id, run.tags, now=now, max_age=max_age, level=level)
        if read is None:
            return None
        found = read.signal
        if run.status == "SUCCESS":
            if unreadable is not None and found.state is HealthState.HEALTHY:
                return QualityEvidence(
                    evaluate_quality(scope, None, now=now, max_age=max_age),
                    unreadable.run_id,
                    unreadable.tags.get(DOMAIN_TAG),
                    None,
                )
            return QualityEvidence(found, run.run_id, run.tags.get(DOMAIN_TAG), read.manifest)
        if found.state in (HealthState.WARNING, HealthState.CRITICAL):
            return QualityEvidence(found, run.run_id, run.tags.get(DOMAIN_TAG), read.manifest)
        if read.unreadable and unreadable is None:
            unreadable = run
    return QualityEvidence(None, None, runs[0].tags.get(DOMAIN_TAG), None)


def _freshness(
    scope: Scope,
    schedule: DagsterSchedule | None,
    runs: list[DagsterRun],
    success: RunObservation | None,
    state: ScheduleState,
    now: datetime,
    options: Freshness | None,
    grace: timedelta,
) -> Signal:
    """Age bounds from the alert policy when it declares them, in any schedule state; otherwise the cadence."""
    if options and (options.warn_after is not None or options.critical_after is not None):
        return evaluate_freshness_age(
            scope, success, now=now, warn_after=options.warn_after, critical_after=options.critical_after
        )
    owed = _owed(schedule, runs, _owed_due(schedule, now, grace))
    return evaluate_freshness(scope, success, owed, now=now, grace=grace, schedule=state)


def _evaluate(
    stage: str,
    job: str,
    runs: list[DagsterRun],
    schedule: DagsterSchedule | None,
    now: datetime,
    quality: QualityReader | None = None,
    conditions: Mapping[str, ResolvedCondition] | None = None,
) -> PipelineEvaluation:
    scope = Scope(stage=stage, product=job)
    missed_options = (conditions or {}).get("missed_run")
    fresh_options = (conditions or {}).get("freshness")
    missed = missed_options.options if missed_options and isinstance(missed_options.options, MissedRun) else None
    freshness = fresh_options.options if fresh_options and isinstance(fresh_options.options, Freshness) else None
    quality_options = (conditions or {}).get("quality")
    level = (
        quality_options.options.level if quality_options and isinstance(quality_options.options, Quality) else "warn"
    )
    grace = missed.grace if missed else SCHEDULE_GRACE
    fresh_grace = freshness.grace if freshness else SCHEDULE_GRACE
    latest = _observation(runs[0]) if runs else None
    # The latest completion, not the newest-created: overlapping runs can finish out of creation order.
    success = max(
        (_observation(run) for run in runs if run.status == "SUCCESS"),
        key=lambda observation: observation.observed_at,
        default=None,
    )
    due = latest_due(schedule.cron_schedule, schedule.timezone, now) if schedule else None
    expected = _expected(schedule, runs, due, now, grace)
    owed = _owed_due(schedule, now, grace)
    max_age = _evidence_bound(schedule, owed, grace)
    if schedule is None:
        state = ScheduleState.ABSENT
    else:
        state = ScheduleState.RUNNING if schedule.status == "RUNNING" else ScheduleState.STOPPED
    signals = [
        evaluate_execution(scope, latest, now=now, max_age=max_age),
        evaluate_latest_success(scope, success, now=now, max_age=max_age),
        evaluate_expected_run(scope, expected, now=now, max_age=SCHEDULE_MAX_AGE, grace=grace),
        _freshness(scope, schedule, runs, success, state, now, freshness, fresh_grace),
    ]
    evidence = _quality(quality, scope, job, runs, now, max_age, level) if quality is not None and runs else None
    if evidence is not None and evidence.signal is not None:
        signals.append(evidence.signal)
    return PipelineEvaluation(job, runs, schedule, due, signals, evidence)


def read_stage_pipelines(
    client: DagsterClient | None,
    stage: str,
    now: datetime,
    quality: QualityReader | None = None,
    policy: ResolvedPolicy | None = None,
) -> tuple[IntegrationStatus, list[PipelineEvaluation]]:
    """The stage's pipelines, and whether Dagster could be read; an unreadable Dagster yields no pipelines.

    With a `quality` reader each product pipeline with a run also carries a quality signal; without one, none.
    With a `policy` each job takes its grace and freshness bounds from the rule that wins for it (ADR 0023, decision
    8); the health routes pass none and keep the cadence-derived signals.
    """
    if client is None:
        absent = IntegrationStatus(name="dagster", status="disabled", reason="no Dagster is configured for this stage")
        return absent, []
    try:
        jobs = [job.name for job in client.list_jobs()]
        schedules = client.list_schedules()
        runs = {job: client.list_runs(job_name=job, limit=RUN_WINDOW) for job in jobs}
    except ServiceClientError as exc:
        # The service's own message goes to the log, not the caller: a Dagster PythonError can carry a stack trace.
        log.warning("Dagster for stage %s could not be read for health: %s", stage, exc)
        return IntegrationStatus(name="dagster", status="unavailable", reason="Dagster could not be read"), []
    # A running schedule wins over a stopped one aimed at the same job: it is written last.
    by_job: dict[str, DagsterSchedule] = {}
    for schedule in sorted(schedules, key=lambda item: item.status == "RUNNING"):
        by_job[schedule.job_name] = schedule
    pipelines = [
        _evaluate(stage, job, runs[job], by_job.get(job), now, quality, policy.for_job(job) if policy else None)
        for job in jobs
    ]
    return IntegrationStatus(name="dagster", status="available", reason="read from Dagster"), pipelines
