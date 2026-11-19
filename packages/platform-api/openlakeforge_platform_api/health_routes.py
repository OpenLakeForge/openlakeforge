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
from typing import cast

from fastapi import HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from openlakeforge_core.health import HealthState, Scope, Signal, rollup, service_signal
from openlakeforge_core.metrics import RANGES
from openlakeforge_services.dagster import DagsterRun

from openlakeforge_platform_api.metrics_routes import metrics_integration
from openlakeforge_platform_api.models import (
    HealthSignal,
    HealthThresholds,
    HealthWindow,
    IntegrationStatus,
    MetricRangeName,
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
    RunWindow,
    at,
    read_stage_pipelines,
)
from openlakeforge_platform_api.reads import _dagster
from openlakeforge_platform_api.service_probes import (
    COMPONENTS,
    PROBES_FAILED_COMPONENT,
    SERVICE_DISPLAY_NAMES,
    StageTargets,
)

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


# The metrics routes' vocabulary (ADR 0024, decision 4), so a page has one window selector. A str rather than the
# Literal those routes take, so that a value outside it is a 400 like the metrics routes' retention refusal, not
# FastAPI's 422; the schema still lists the values.
_RANGE = Query(
    "24h",
    alias="range",
    description="The window the counts cover, ending now. The state and its signals are never windowed.",
    json_schema_extra={"enum": list(RANGES)},
)


def _since(range_name: str, now: datetime) -> datetime:
    metric_range = RANGES.get(range_name)
    if metric_range is None:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(RANGES)}")
    return now - metric_range.duration


def _window(range_name: str, now: datetime, since: datetime, pipelines: list[PipelineEvaluation]) -> HealthWindow:
    # The latest of the jobs' own: the time from which every job's count is complete.
    covered = [window.covered_from for window in map(_windowed, pipelines) if window.covered_from]
    return HealthWindow(
        range=cast(MetricRangeName, range_name), from_=since, to=now, covered_from=max(covered, default=None)
    )


def _failed_runs(dagster: IntegrationStatus, pipelines: list[PipelineEvaluation]) -> int | None:
    # No read is no count: a Dagster that is absent or unreadable has no pipelines, which must not read as 0 failures.
    if dagster.status != "available":
        return None
    return sum(_windowed(pipeline).failed_runs for pipeline in pipelines)


def _windowed(pipeline: PipelineEvaluation) -> RunWindow:
    # `_read` is always given `since` on these routes, and `read_stage_pipelines` then sets every pipeline's window.
    assert pipeline.window is not None
    return pipeline.window


def _read(
    request: Request, stage: str, now: datetime, since: datetime | None = None
) -> tuple[IntegrationStatus, list[PipelineEvaluation]]:
    configured = request.app.state.settings.dagster_graphql_urls.get(stage)
    return read_stage_pipelines(
        _dagster(request, stage) if configured else None,
        stage,
        now,
        request.app.state.quality_readers.get(stage),
        since=since,
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
        due=signal.due,
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
            PROBES_FAILED_COMPONENT,
            HealthState.UNKNOWN,
            "the service probes failed",
            now=now,
            max_age=SERVICE_MAX_AGE,
        )
        return IntegrationStatus(name="services", status="unavailable", reason="service probes failed"), [failed]
    return IntegrationStatus(name="services", status="available", reason="probed in the stage namespace"), signals


def _service(signal: Signal) -> ServiceHealth:
    component = signal.component or "unknown"
    # No fallback to the raw id: a component without a label is a probe added without one, which a test catches.
    label = COMPONENTS[component]
    observed, expires = signal.observed_at, signal.expires_at
    return ServiceHealth(
        component=component,
        source=label.source,
        max_age_seconds=int((expires - observed).total_seconds()) if observed and expires else None,
        state=signal.state.value,
        reason=signal.reason,
        observed_at=signal.observed_at,
        expires_at=signal.expires_at,
        display_name=label.display_name,
        service=label.service,
        service_display_name=SERVICE_DISPLAY_NAMES[label.service],
        last_success_at=signal.last_success_at,
    )


async def get_stage_health(request: Request, stage: str, range_name: str = _RANGE) -> StageHealth:
    """A rollup of the stage's pipeline and service signals. Integrations that give none are never healthy.

    `range` scopes the failed-run counts only; the state is the current evaluation at every range.

    Every read blocks (Dagster's HTTP, the probes and the metrics backend for up to their timeout), so each runs on a
    worker thread, together: the event loop keeps serving other requests, and the stage answers in the slowest read's
    time. The metrics integration is reported, never folded into the state.
    """
    now = datetime.now(UTC)
    since = _since(range_name, now)
    (dagster, pipelines), (services, service_signals), metrics = await asyncio.gather(
        run_in_threadpool(_read, request, stage, now, since),
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
        window=_window(range_name, now, since, pipelines),
        failed_runs=_failed_runs(dagster, pipelines),
        pipelines=[
            StagePipelineState(
                job=pipeline.job,
                domain=pipeline.domain,
                product=pipeline.product,
                state=pipeline.state.value,
                failed_runs=_windowed(pipeline).failed_runs,
            )
            for pipeline in pipelines
        ],
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


def list_pipeline_health(request: Request, stage: str, range_name: str = _RANGE) -> PipelinesHealth:
    """Each Dagster job's latest run, last success, failed runs in the window, schedule and freshness."""
    now = datetime.now(UTC)
    since = _since(range_name, now)
    dagster, pipelines = _read(request, stage, now, since)
    if dagster.status == "disabled":
        raise HTTPException(status_code=404, detail=f"no Dagster is configured for stage {stage!r}")
    return PipelinesHealth(
        stage=stage,
        evaluated_at=now,
        window=_window(range_name, now, since, pipelines),
        failed_runs=_failed_runs(dagster, pipelines),
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
    window = _windowed(pipeline)
    success = next((run for run in pipeline.runs if run.status == "SUCCESS"), None)
    success_at = at(success.ended_at or success.created_at) if success else None
    schedule = pipeline.schedule
    return PipelineHealth(
        job=pipeline.job,
        domain=pipeline.domain,
        product=pipeline.product,
        state=pipeline.state.value,
        latest_run=_run(pipeline.runs[0]) if pipeline.runs else None,
        last_success=_run(success) if success else None,
        failed_runs=window.failed_runs,
        covered_from=window.covered_from,
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
