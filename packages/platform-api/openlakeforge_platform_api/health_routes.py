"""Stage and pipeline health read models, built from Dagster reads and classified by `openlakeforge_core.health`.

Nothing is read that a caller could not already read through the runs routes, and the answer carries no run tags,
logs or error text: run ids, statuses and times only. The matrix row is the only control, so these rows take the
runs routes' floor (permissions.toml). A read that fails or returns nothing is `unknown`, never `healthy`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
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
    evaluate_latest_success,
    rollup,
)
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterRun, DagsterSchedule

from openlakeforge_platform_api.models import (
    HealthSignal,
    HealthThresholds,
    IntegrationStatus,
    PipelineFreshness,
    PipelineHealth,
    PipelineSchedule,
    PipelinesHealth,
    Run,
    StageHealth,
    StagePipelineState,
)
from openlakeforge_platform_api.reads import _dagster

log = logging.getLogger(__name__)

# Provisional (#1127): a daily product schedule leaves a day of slack and a weekly one still reads healthy. A manual
# pipeline untouched for longer reads `unknown`, which is the honest answer about it.
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
_NOT_PROVIDED = [
    IntegrationStatus(name="services", status="unavailable", reason="service probes are not yet provided"),
    IntegrationStatus(name="quality", status="unavailable", reason="quality summaries are not yet provided"),
    IntegrationStatus(name="metrics", status="unavailable", reason="no metrics source is connected to the API"),
]


@dataclass(frozen=True)
class _Pipeline:
    job: str
    runs: list[DagsterRun]
    schedule: DagsterSchedule | None
    signals: list[Signal]

    @property
    def state(self) -> HealthState:
        return rollup(self.signals)


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _observation(run: DagsterRun) -> RunObservation:
    return RunObservation(
        status=_RUN_STATUS.get(run.status, RunStatus.RUNNING),
        observed_at=_at(run.ended_at or run.started_at or run.created_at),
        run=run.run_id,
    )


def _evaluate(
    stage: str, job: str, runs: list[DagsterRun], schedule: DagsterSchedule | None, now: datetime
) -> _Pipeline:
    scope = Scope(stage=stage, product=job)
    latest = _observation(runs[0]) if runs else None
    success = next((_observation(run) for run in runs if run.status == "SUCCESS"), None)
    if schedule is None:
        expected = ScheduleObservation(ScheduleState.ABSENT, now)
    else:
        expected = ScheduleObservation(
            ScheduleState.RUNNING if schedule.status == "RUNNING" else ScheduleState.STOPPED,
            now,
            last_expected_at=_at(schedule.last_tick_at) if schedule.last_tick_at is not None else None,
            # Any run counts, a manual one included: Dagster does not say which run a tick requested.
            last_run_started_at=_at(runs[0].created_at) if runs else None,
        )
    signals = [
        evaluate_execution(scope, latest, now=now, max_age=RUN_MAX_AGE),
        evaluate_latest_success(scope, success, now=now, max_age=RUN_MAX_AGE),
        evaluate_expected_run(scope, expected, now=now, max_age=SCHEDULE_MAX_AGE, grace=SCHEDULE_GRACE),
    ]
    return _Pipeline(job, runs, schedule, signals)


def _read(request: Request, stage: str, now: datetime) -> tuple[IntegrationStatus, list[_Pipeline]]:
    """The stage's pipelines, and whether Dagster could be read; an unreadable Dagster yields no pipelines."""
    if not request.app.state.settings.dagster_graphql_urls.get(stage):
        absent = IntegrationStatus(name="dagster", status="disabled", reason="no Dagster is configured for this stage")
        return absent, []
    client = _dagster(request, stage)
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
    pipelines = [_evaluate(stage, job, runs[job], by_job.get(job), now) for job in jobs]
    return IntegrationStatus(name="dagster", status="available", reason="read from Dagster"), pipelines


def _run(run: DagsterRun) -> Run:
    # Tags can carry launch configuration and operator names; health needs none of them.
    return Run(**{**asdict(run), "tags": {}})


def _signal(signal: Signal) -> HealthSignal:
    return HealthSignal(
        kind=signal.kind.value,
        state=signal.state.value,
        reason=signal.reason,
        run=signal.run,
        observed_at=signal.observed_at,
        expires_at=signal.expires_at,
    )


def get_stage_health(request: Request, stage: str) -> StageHealth:
    """A rollup of the stage's pipeline signals. Integrations that give none are listed, never counted as healthy."""
    now = datetime.now(UTC)
    dagster, pipelines = _read(request, stage, now)
    return StageHealth(
        stage=stage,
        state=rollup(signal for pipeline in pipelines for signal in pipeline.signals).value,
        evaluated_at=now,
        pipelines=[StagePipelineState(job=pipeline.job, state=pipeline.state.value) for pipeline in pipelines],
        integrations=[dagster, *_NOT_PROVIDED],
    )


def list_pipeline_health(request: Request, stage: str) -> PipelinesHealth:
    """Each Dagster job's latest run, last success, failed runs, schedule and freshness."""
    now = datetime.now(UTC)
    dagster, pipelines = _read(request, stage, now)
    if dagster.status == "disabled":
        raise HTTPException(status_code=404, detail=f"no Dagster is configured for stage {stage!r}")
    return PipelinesHealth(
        stage=stage,
        evaluated_at=now,
        thresholds=HealthThresholds(
            run_max_age_seconds=int(RUN_MAX_AGE.total_seconds()),
            schedule_max_age_seconds=int(SCHEDULE_MAX_AGE.total_seconds()),
            schedule_grace_seconds=int(SCHEDULE_GRACE.total_seconds()),
            run_window=RUN_WINDOW,
        ),
        integrations=[dagster, *_NOT_PROVIDED],
        pipelines=[_pipeline(pipeline, now) for pipeline in pipelines],
    )


def _pipeline(pipeline: _Pipeline, now: datetime) -> PipelineHealth:
    success = next((run for run in pipeline.runs if run.status == "SUCCESS"), None)
    success_at = _at(success.ended_at or success.created_at) if success else None
    schedule = pipeline.schedule
    return PipelineHealth(
        job=pipeline.job,
        state=pipeline.state.value,
        latest_run=_run(pipeline.runs[0]) if pipeline.runs else None,
        last_success=_run(success) if success else None,
        failed_runs=sum(run.status == "FAILURE" for run in pipeline.runs),
        schedule=PipelineSchedule(
            name=schedule.name if schedule else None,
            status="absent" if schedule is None else ("running" if schedule.status == "RUNNING" else "stopped"),
            last_expected_at=_at(schedule.last_tick_at) if schedule and schedule.last_tick_at is not None else None,
        ),
        freshness=PipelineFreshness(
            last_success_at=success_at, age_seconds=int((now - success_at).total_seconds()) if success_at else None
        ),
        signals=[_signal(signal) for signal in pipeline.signals],
    )
