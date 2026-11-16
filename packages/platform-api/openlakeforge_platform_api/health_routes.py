"""Stage and pipeline health read models, built from Dagster reads and classified by `openlakeforge_core.health`.

Nothing is read that a caller could not already read through the runs routes, and the answer carries no run tags,
logs or error text: run ids, statuses and times only. The matrix row is the only control, so these rows take the
runs routes' floor (permissions.toml). A read that fails or returns nothing is `unknown`, never `healthy`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
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
    service_signal,
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
    ServiceHealth,
    StageHealth,
    StagePipelineState,
)
from openlakeforge_platform_api.reads import _dagster
from openlakeforge_platform_api.schedules import latest_due
from openlakeforge_platform_api.service_probes import StageTargets

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
# A service signal outlives one evaluation by this long, so a consumer can tell a stale answer from a fresh one.
SERVICE_MAX_AGE = timedelta(seconds=60)
_SERVICES_ELSEWHERE = IntegrationStatus(
    name="services", status="unavailable", reason="service signals are served by the stage health route"
)
_NOT_PROVIDED = [
    IntegrationStatus(name="quality", status="unavailable", reason="quality summaries are not yet provided"),
    IntegrationStatus(name="metrics", status="unavailable", reason="no metrics source is connected to the API"),
]


@dataclass(frozen=True)
class _Pipeline:
    job: str
    runs: list[DagsterRun]
    schedule: DagsterSchedule | None
    # When the schedule's cron last came due; None without a schedule or when its cron cannot be read.
    due: datetime | None
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
        return ScheduleObservation(ScheduleState.STOPPED, now)
    if due is None:
        return None
    # Any run counts, a manual one included: Dagster does not say which run a tick requested.
    last_run = _at(runs[0].created_at) if runs else None
    if schedule.started_at is not None:
        # An occurrence before the schedule was switched on was never expected.
        if due < _at(schedule.started_at):
            return ScheduleObservation(ScheduleState.RUNNING, now, last_run_started_at=last_run)
    elif schedule.last_tick_at is None and now >= due + SCHEDULE_GRACE and not (last_run and last_run >= due):
        # Dagster stores no start time for a schedule that is on by default in code. With no tick either, nothing
        # shows the schedule was on at its due time (it may have been deployed after it), so a missing run is not
        # evidence: `unknown`, never `healthy`.
        return None
    return ScheduleObservation(ScheduleState.RUNNING, now, last_expected_at=due, last_run_started_at=last_run)


def _evaluate(
    stage: str, job: str, runs: list[DagsterRun], schedule: DagsterSchedule | None, now: datetime
) -> _Pipeline:
    scope = Scope(stage=stage, product=job)
    latest = _observation(runs[0]) if runs else None
    success = next((_observation(run) for run in runs if run.status == "SUCCESS"), None)
    due = latest_due(schedule.cron_schedule, schedule.timezone, now) if schedule else None
    expected = _expected(schedule, runs, due, now)
    signals = [
        evaluate_execution(scope, latest, now=now, max_age=RUN_MAX_AGE),
        evaluate_latest_success(scope, success, now=now, max_age=RUN_MAX_AGE),
        evaluate_expected_run(scope, expected, now=now, max_age=SCHEDULE_MAX_AGE, grace=SCHEDULE_GRACE),
    ]
    return _Pipeline(job, runs, schedule, due, signals)


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


def _probe(request: Request, stage: str, now: datetime) -> tuple[IntegrationStatus, list[Signal]]:
    """The stage's service signals. Blocks for up to the probes' timeout, so it only ever runs on a worker thread."""
    settings = request.app.state.settings
    prober = request.app.state.service_probes
    namespace = settings.stage_namespaces.get(stage)
    if prober is None or namespace is None:
        return IntegrationStatus(name="services", status="disabled", reason="no service probes are configured"), []
    buckets = settings.file_buckets.get(stage)
    targets = StageTargets(
        stage=stage,
        namespace=namespace,
        dagster_graphql_url=settings.dagster_graphql_urls.get(stage),
        trino_url=settings.trino_urls.get(stage),
        bucket=buckets[0] if buckets else None,
    )
    try:
        signals: list[Signal] = prober.probe(targets)
    except Exception:  # noqa: BLE001 - a probe that could not look must read unknown, never fail the whole answer
        log.warning("service probes for stage %s failed", stage, exc_info=True)
        failed = service_signal(
            Scope(stage=stage),
            "service-probes",
            HealthState.UNKNOWN,
            "the service probes failed",
            now=now,
            max_age=SERVICE_MAX_AGE,
        )
        return IntegrationStatus(name="services", status="unavailable", reason="service probes failed"), [failed]
    return IntegrationStatus(name="services", status="available", reason="probed in the stage namespace"), signals


def _service(signal: Signal) -> ServiceHealth:
    return ServiceHealth(
        component=signal.component or "unknown",
        state=signal.state.value,
        reason=signal.reason,
        observed_at=signal.observed_at,
        expires_at=signal.expires_at,
    )


async def get_stage_health(request: Request, stage: str) -> StageHealth:
    """A rollup of the stage's pipeline and service signals. Integrations that give none are never healthy.

    Both reads block (Dagster's HTTP, and the probes for up to their timeout), so each runs on a worker thread,
    together: the event loop keeps serving other requests, and the stage answers in the slower read's time.
    """
    now = datetime.now(UTC)
    (dagster, pipelines), (services, service_signals) = await asyncio.gather(
        run_in_threadpool(_read, request, stage, now), run_in_threadpool(_probe, request, stage, now)
    )
    signals = [signal for pipeline in pipelines for signal in pipeline.signals] + service_signals
    if dagster.status == "unavailable":
        # No pipelines means no signal: without this a configured Dagster that cannot be read would drop out of the
        # rollup and a stage with passing service probes would read healthy.
        signals.append(
            service_signal(
                Scope(stage=stage),
                "dagster.pipelines",
                HealthState.UNKNOWN,
                "Dagster could not be read",
                now=now,
                max_age=SERVICE_MAX_AGE,
            )
        )
    return StageHealth(
        stage=stage,
        state=rollup(signals).value,
        evaluated_at=now,
        pipelines=[StagePipelineState(job=pipeline.job, state=pipeline.state.value) for pipeline in pipelines],
        services=[_service(signal) for signal in service_signals],
        integrations=[dagster, services, *_NOT_PROVIDED],
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
        integrations=[dagster, _SERVICES_ELSEWHERE, *_NOT_PROVIDED],
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
            last_expected_at=pipeline.due,
        ),
        freshness=PipelineFreshness(
            last_success_at=success_at, age_seconds=int((now - success_at).total_seconds()) if success_at else None
        ),
        signals=[_signal(signal) for signal in pipeline.signals],
    )
