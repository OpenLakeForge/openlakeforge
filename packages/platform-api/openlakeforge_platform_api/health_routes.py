"""Stage and pipeline health read models, built from Dagster reads and classified by `openlakeforge_core.health`.

Nothing is read that a caller could not already read through the runs routes, and the answer carries no run tags,
logs or error text: run ids, statuses and times only. The matrix row is the only control, so these rows take the
runs routes' floor (permissions.toml). A read that fails or returns nothing is `unknown`, never `healthy`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from openlakeforge_core.health import HealthState, Scope, Signal, rollup, service_signal
from openlakeforge_services.dagster import DagsterRun

from openlakeforge_platform_api.metrics_routes import metrics_integration
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
    StageServiceHealth,
)
from openlakeforge_platform_api.pipeline_health import (
    RUN_MAX_AGE,
    RUN_WINDOW,
    SCHEDULE_GRACE,
    SCHEDULE_MAX_AGE,
    PipelineEvaluation,
    at,
    read_stage_pipelines,
)
from openlakeforge_platform_api.reads import _dagster
from openlakeforge_platform_api.service_probes import COMPONENT_SOURCES, StageTargets

log = logging.getLogger(__name__)

# A service signal outlives one evaluation by this long, so a consumer can tell a stale answer from a fresh one.
SERVICE_MAX_AGE = timedelta(seconds=60)
_SERVICES_ELSEWHERE = IntegrationStatus(
    name="services", status="unavailable", reason="service signals are served by the stage health route"
)


def _quality(request: Request, stage: str) -> IntegrationStatus:
    if request.app.state.quality_readers.get(stage) is None:
        return IntegrationStatus(
            name="quality", status="not_provided", reason="no quality manifest reader is configured for this stage"
        )
    return IntegrationStatus(name="quality", status="available", reason="read from the stage's quality manifests")


def _quality_sync(request: Request, stage: str) -> IntegrationStatus:
    """Publication to OpenMetadata, apart from the data's quality. A stage without a reconciler has no publisher at
    all, which is not a failed one."""
    if stage not in request.app.state.settings.quality_sync_summaries:
        return IntegrationStatus(
            name="quality-sync", status="not_provided", reason="no quality results are published from this stage"
        )
    if request.app.state.service_probes is None:
        return IntegrationStatus(name="quality-sync", status="unavailable", reason="no service probes are configured")
    return IntegrationStatus(
        name="quality-sync", status="available", reason="read from the reconciler's quality-sync summary"
    )


def _read(request: Request, stage: str, now: datetime) -> tuple[IntegrationStatus, list[PipelineEvaluation]]:
    configured = request.app.state.settings.dagster_graphql_urls.get(stage)
    return read_stage_pipelines(
        _dagster(request, stage) if configured else None, stage, now, request.app.state.quality_readers.get(stage)
    )


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
    archive = settings.log_archives.get(stage)
    targets = StageTargets(
        stage=stage,
        namespace=namespace,
        dagster_graphql_url=settings.dagster_graphql_urls.get(stage),
        trino_url=settings.trino_urls.get(stage),
        polaris_health_url=settings.polaris_health_url,
        bucket=buckets[0] if buckets else None,
        log_archive_base_uri=archive.base_uri if archive else None,
        log_archive_namespace=archive.namespace if archive else None,
        spool_probe_url=settings.log_collector_probe_url,
        quality_sync_summary_uri=settings.quality_sync_summaries.get(stage),
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
    component = signal.component or "unknown"
    observed, expires = signal.observed_at, signal.expires_at
    return ServiceHealth(
        component=component,
        source=COMPONENT_SOURCES.get(component, "platform-api"),
        max_age_seconds=int((expires - observed).total_seconds()) if observed and expires else None,
        state=signal.state.value,
        reason=signal.reason,
        observed_at=signal.observed_at,
        expires_at=signal.expires_at,
    )


async def get_stage_health(request: Request, stage: str) -> StageHealth:
    """A rollup of the stage's pipeline and service signals. Integrations that give none are never healthy.

    Every read blocks (Dagster's HTTP, the probes and the metrics backend for up to their timeout), so each runs on a
    worker thread, together: the event loop keeps serving other requests, and the stage answers in the slowest read's
    time. The metrics integration is reported, never folded into the state.
    """
    now = datetime.now(UTC)
    (dagster, pipelines), (services, service_signals), metrics = await asyncio.gather(
        run_in_threadpool(_read, request, stage, now),
        run_in_threadpool(_probe, request, stage, now),
        run_in_threadpool(metrics_integration, request),
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
        integrations=[
            dagster,
            services,
            _quality(request, stage),
            _quality_sync(request, stage),
            metrics,
        ],
    )


async def get_stage_service_health(request: Request, stage: str) -> StageServiceHealth:
    """The stage's service probes and nothing else.

    No Dagster read runs here, so the answer takes about the probes' own timeout (`PROBE_TIMEOUT`) even while Dagster
    hangs. The alert evaluator reads this route and not `get_stage_health`, whose pipeline reads are unbounded across
    calls: during a Dagster outage that route would outlast the evaluator and no service incident could open.
    """
    _, signals = await run_in_threadpool(_probe, request, stage, datetime.now(UTC))
    return StageServiceHealth(stage=stage, services=[_service(signal) for signal in signals])


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
        integrations=[dagster, _SERVICES_ELSEWHERE, _quality(request, stage), metrics_integration(request)],
        pipelines=[_pipeline(pipeline, now) for pipeline in pipelines],
    )


def _pipeline(pipeline: PipelineEvaluation, now: datetime) -> PipelineHealth:
    success = next((run for run in pipeline.runs if run.status == "SUCCESS"), None)
    success_at = at(success.ended_at or success.created_at) if success else None
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
