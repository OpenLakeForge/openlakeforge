"""Per-stage pipeline health from Dagster reads, classified by `openlakeforge_core.health`.

Takes a Dagster client rather than a request so a caller outside the HTTP app, such as a CronJob, can reuse it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
    evaluate_latest_success,
    rollup,
)
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient, DagsterRun, DagsterSchedule

from openlakeforge_platform_api.models import IntegrationStatus
from openlakeforge_platform_api.quality_health import QualityReader, quality_signal
from openlakeforge_platform_api.schedules import latest_due, next_due

log = logging.getLogger(__name__)

# Provisional (#1127), and only for a pipeline with no running schedule: a manual pipeline untouched for longer reads
# `unknown`, which is the honest answer about it. A scheduled pipeline's bound is its own cadence (`_run_max_age`).
RUN_MAX_AGE = timedelta(days=7)
# The schedule is read as it is evaluated, so its max_age only matters to the evaluator's contract.
SCHEDULE_MAX_AGE = timedelta(minutes=5)
# Dagster's scheduler ticks once a minute and a run is created after the tick; this leaves room for a slow queue.
SCHEDULE_GRACE = timedelta(minutes=15)
# Runs read per pipeline: enough to find a recent success and count failures without paging.
RUN_WINDOW = 25

_RUN_STATUS = {
    "SUCCESS": RunStatus.SUCCEEDED,
    "FAILURE": RunStatus.FAILED,
    "CANCELED": RunStatus.CANCELED,
    "CANCELING": RunStatus.CANCELED,
}


@dataclass(frozen=True)
class PipelineEvaluation:
    job: str
    runs: list[DagsterRun]
    schedule: DagsterSchedule | None
    # When the schedule's cron last came due; None without a schedule or when its cron cannot be read.
    due: datetime | None
    signals: list[Signal]

    @property
    def state(self) -> HealthState:
        return rollup(self.signals)


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _observation(run: DagsterRun) -> RunObservation:
    return RunObservation(
        status=_RUN_STATUS.get(run.status, RunStatus.RUNNING),
        observed_at=at(run.ended_at or run.started_at or run.created_at),
        run=run.run_id,
    )


def _expected(
    schedule: DagsterSchedule | None, runs: list[DagsterRun], due: datetime | None, now: datetime
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
    elif schedule.last_tick_at is None and now >= due + SCHEDULE_GRACE and not (last_run and last_run >= due):
        # Dagster stores no start time for a schedule that is on by default in code. With no tick either, nothing
        # shows the schedule was on at its due time (it may have been deployed after it), so a missing run is not
        # evidence: `unknown`, never `healthy`.
        return None
    return ScheduleObservation(ScheduleState.RUNNING, now, last_expected_at=due, last_run_started_at=last_run)


def _owed_due(schedule: DagsterSchedule | None, now: datetime) -> datetime | None:
    """The latest due time at or before `now - SCHEDULE_GRACE`, from the cron alone: the run that should have succeeded.

    Taken straight from the cron rather than stepped back from the latest due time, so a schedule that fires more
    often than the grace (every minute, every five) still owes a run: stepping back one occurrence lands inside the
    grace again. None where the schedule is not running or its cron cannot be read.
    """
    if schedule is None or schedule.status != "RUNNING":
        return None
    return latest_due(schedule.cron_schedule, schedule.timezone, now - SCHEDULE_GRACE)


def _evidence_bound(schedule: DagsterSchedule | None, owed: datetime | None) -> timedelta:
    """How long a run stays evidence: the gap from the owed due time to the next one, plus the grace.

    A success stays valid until the next due time after it, so a weekday cron's Friday success is still evidence on
    Saturday and Sunday. Without a running schedule, or when the cron cannot be read, the provisional seven days.
    """
    if schedule is None or owed is None:
        return RUN_MAX_AGE
    following = next_due(schedule.cron_schedule, schedule.timezone, owed)
    return following - owed + SCHEDULE_GRACE if following else RUN_MAX_AGE


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
    quality: QualityReader, scope: Scope, job: str, runs: list[DagsterRun], now: datetime, max_age: timedelta
) -> Signal | None:
    """The quality signal of the latest run's manifest, where a failed run's blocking errors are.

    The manifest is published after every step and omits dbt's results until dbt's evidence is read, so one from a
    run that did not succeed (in progress, canceled, or failed before dbt reported) may lack the checks still to
    come, and a pass in it proves nothing: a retry that has cleared Silver would resolve the incident the previous
    run's dbt failure opened. Only a successful run's manifest can read `healthy`. Any other run contributes only
    a warning or critical; otherwise the latest successful run's manifest answers, and with none there is no signal,
    which leaves an open incident open.
    """
    latest = runs[0]
    found = quality_signal(quality, scope, job, latest.run_id, latest.tags, now=now, max_age=max_age)
    if latest.status == "SUCCESS" or found is None or found.state in (HealthState.WARNING, HealthState.CRITICAL):
        return found
    succeeded = next((run for run in runs[1:] if run.status == "SUCCESS"), None)
    if succeeded is None:
        return None
    return quality_signal(quality, scope, job, succeeded.run_id, succeeded.tags, now=now, max_age=max_age)


def _evaluate(
    stage: str,
    job: str,
    runs: list[DagsterRun],
    schedule: DagsterSchedule | None,
    now: datetime,
    quality: QualityReader | None = None,
) -> PipelineEvaluation:
    scope = Scope(stage=stage, product=job)
    latest = _observation(runs[0]) if runs else None
    # The latest completion, not the newest-created: overlapping runs can finish out of creation order.
    success = max(
        (_observation(run) for run in runs if run.status == "SUCCESS"),
        key=lambda observation: observation.observed_at,
        default=None,
    )
    due = latest_due(schedule.cron_schedule, schedule.timezone, now) if schedule else None
    expected = _expected(schedule, runs, due, now)
    owed = _owed_due(schedule, now)
    max_age = _evidence_bound(schedule, owed)
    if schedule is None:
        state = ScheduleState.ABSENT
    else:
        state = ScheduleState.RUNNING if schedule.status == "RUNNING" else ScheduleState.STOPPED
    signals = [
        evaluate_execution(scope, latest, now=now, max_age=max_age),
        evaluate_latest_success(scope, success, now=now, max_age=max_age),
        evaluate_expected_run(scope, expected, now=now, max_age=SCHEDULE_MAX_AGE, grace=SCHEDULE_GRACE),
        evaluate_freshness(scope, success, _owed(schedule, runs, owed), now=now, grace=SCHEDULE_GRACE, schedule=state),
    ]
    if quality is not None and runs:
        found = _quality(quality, scope, job, runs, now, max_age)
        if found is not None:
            signals.append(found)
    return PipelineEvaluation(job, runs, schedule, due, signals)


def read_stage_pipelines(
    client: DagsterClient | None, stage: str, now: datetime, quality: QualityReader | None = None
) -> tuple[IntegrationStatus, list[PipelineEvaluation]]:
    """The stage's pipelines, and whether Dagster could be read; an unreadable Dagster yields no pipelines.

    With a `quality` reader each product pipeline with a run also carries a quality signal; without one, none.
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
    pipelines = [_evaluate(stage, job, runs[job], by_job.get(job), now, quality) for job in jobs]
    return IntegrationStatus(name="dagster", status="available", reason="read from Dagster"), pipelines
