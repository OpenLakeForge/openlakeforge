"""The metrics read path (ADR 0024, decision 4): named series from the catalog over a fixed range, never a query.

Every query text is built here from a catalog entry, an enabled stage the matrix already checked, and a range from
`RANGES`; nothing a caller sends reaches Prometheus. A backend the profile turns off is `disabled`, a provider without
one `not_provided`, and a Prometheus that does not answer, a series with no samples or targets that were down for the
whole range `unavailable`: never a line of zeros. Metrics never feed `HealthState`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException, Query, Request
from openlakeforge_core.metrics import (
    RANGES,
    MetricRange,
    MetricScope,
    MetricSeries,
    MetricSource,
    StepAggregation,
    catalog_for,
    find_series,
)
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient
from openlakeforge_services.prometheus import PrometheusClient, PrometheusSeries

from openlakeforge_platform_api.models import (
    IntegrationStatus,
    MetricCatalog,
    MetricLine,
    MetricRangeName,
    MetricSeriesData,
    MetricSeriesInfo,
    MetricState,
)
from openlakeforge_platform_api.pipeline_series import read_pipeline_series
from openlakeforge_platform_api.settings import Settings

log = logging.getLogger(__name__)

# The `stage` label value of the nodes and olf-system components (ADR 0024, decision 3).
PLATFORM_STAGE = "platform"
# The health routes' bound on the metrics probe, as on the service probes (PROBE_TIMEOUT): the whole call, any wait
# included. The client's own timeout bounds each socket read, not the call, so the deadline is held here.
PROBE_DEADLINE_SECONDS = 5.0
# A probe abandoned at the deadline keeps its thread until the client's timeout ends it; a small pool caps how many.
_PROBES = ThreadPoolExecutor(max_workers=4, thread_name_prefix="metrics-probe")


def _backend(settings: Settings) -> PrometheusClient | IntegrationStatus:
    """The client for the configured backend, or why none is asked at all."""
    if not settings.metrics_enabled:
        return IntegrationStatus(name="metrics", status="disabled", reason="the deployment profile turns metrics off")
    if settings.metrics_url is None:
        return IntegrationStatus(name="metrics", status="not_provided", reason="the provider offers no metrics backend")
    return PrometheusClient(settings.metrics_url)


def metrics_integration(request: Request) -> IntegrationStatus:
    """The health routes' `metrics` integration: whether the backend answers a query now.

    One attempt, no retry after a 429, and never longer than PROBE_DEADLINE_SECONDS: the stage health route awaits
    it, so a slow or rate-limiting backend must read `unavailable`, not hold up the health answer.
    """
    settings: Settings = request.app.state.settings
    backend = _backend(settings)
    if isinstance(backend, IntegrationStatus):
        return backend
    probe = PrometheusClient(str(settings.metrics_url), timeout=PROBE_DEADLINE_SECONDS, rate_limit_retries=0)
    answer = _PROBES.submit(probe.query, "vector(1)", time=datetime.now(UTC).timestamp())
    try:
        answer.result(timeout=PROBE_DEADLINE_SECONDS)
    except FutureTimeout:
        log.warning("the metrics backend did not answer within %s s", PROBE_DEADLINE_SECONDS)
        return IntegrationStatus(
            name="metrics",
            status="unavailable",
            reason=f"the metrics backend did not answer within {PROBE_DEADLINE_SECONDS:g} s",
        )
    except ServiceClientError as exc:
        log.warning("the metrics backend could not be read: %s", exc)
        return IntegrationStatus(name="metrics", status="unavailable", reason="the metrics backend did not answer")
    return IntegrationStatus(name="metrics", status="available", reason="the metrics backend answered a query")


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _expression(series: MetricSeries, stage: str, metric_range: MetricRange) -> str:
    matcher = f"{{stage={_quoted(stage)}}}" if "stage" in series.labels else ""
    selector = f"{series.rule}{matcher}"
    if series.aggregation is StepAggregation.NONE:
        return selector
    # Each point is the value over the step that ends at it: a counter's increase, or the sum of the increments the
    # rule recorded within it.
    return f"{series.aggregation.value}({selector}[{int(metric_range.step.total_seconds())}s])"


def _targets_expression(series: MetricSeries, stage: str, metric_range: MetricRange) -> str:
    jobs = "|".join(series.jobs)
    stage_matcher = f",stage={_quoted(stage)}" if series.per_stage_targets else ""
    return f'max_over_time(up{{job=~"{jobs}"{stage_matcher}}}[{int(metric_range.duration.total_seconds())}s])'


def _end(metric_range: MetricRange, now: datetime) -> datetime:
    # On a step boundary, so the points of two reads a minute apart line up and a step is never half-evaluated.
    step = int(metric_range.step.total_seconds())
    return datetime.fromtimestamp(int(now.timestamp()) // step * step, UTC)


def _times(metric_range: MetricRange, end: datetime) -> list[datetime]:
    return [end - metric_range.step * (metric_range.points - 1 - index) for index in range(metric_range.points)]


def _line(series: MetricSeries, found: PrometheusSeries, times: list[datetime]) -> MetricLine:
    values = {round(time): value for time, value in found.samples}
    return MetricLine(
        labels={name: value for name, value in found.labels.items() if name in series.labels},
        points=[(time, values.get(round(time.timestamp()))) for time in times],
    )


def _envelope(
    series: MetricSeries,
    stage: str | None,
    metric_range: MetricRange,
    now: datetime,
    state: MetricState,
    reason: str,
    lines: list[MetricLine] | None = None,
    *,
    complete: bool = True,
    covered_from: datetime | None = None,
) -> MetricSeriesData:
    return MetricSeriesData(
        series=series.name,
        stage=stage,
        source=series.source.value,
        state=state,
        reason=reason,
        range=cast(MetricRangeName, metric_range.name),
        step_seconds=int(metric_range.step.total_seconds()),
        unit=series.unit,
        observed_at=now,
        complete=complete,
        covered_from=covered_from,
        lines=lines or [],
    )


def _read_prometheus(
    settings: Settings,
    series: MetricSeries,
    stage: str,
    metric_range: MetricRange,
    now: datetime,
    path_stage: str | None,
) -> MetricSeriesData:
    client = _backend(settings)
    if isinstance(client, IntegrationStatus):
        return _envelope(series, path_stage, metric_range, now, client.status, client.reason)
    end = _end(metric_range, now)
    times = _times(metric_range, end)
    try:
        targets = client.query(_targets_expression(series, stage, metric_range), time=end.timestamp())
        found = client.query_range(
            _expression(series, stage, metric_range),
            start=times[0].timestamp(),
            end=end.timestamp(),
            step=metric_range.step.total_seconds(),
        )
    except ServiceClientError as exc:
        log.warning("metric %s for %s could not be read: %s", series.name, stage, exc)
        return _envelope(series, path_stage, metric_range, now, "unavailable", "the metrics backend did not answer")
    if not any(target.samples[0][1] for target in targets):
        reason = "every target behind the series was down for the whole range" if targets else "no target is scraped"
        return _envelope(series, path_stage, metric_range, now, "unavailable", reason)
    if not found:
        return _envelope(
            series, path_stage, metric_range, now, "unavailable", "the metrics backend holds no samples for the range"
        )
    lines = [_line(series, item, times) for item in found]
    return _envelope(series, path_stage, metric_range, now, "available", "read from the metrics backend", lines)


def _read_dagster(
    request: Request, series: MetricSeries, stage: str, metric_range: MetricRange, now: datetime
) -> MetricSeriesData:
    url = request.app.state.settings.dagster_graphql_urls.get(stage)
    read = read_pipeline_series(
        DagsterClient(url) if url else None,
        series.name,
        stage,
        end=_end(metric_range, now),
        duration=metric_range.duration,
        step=metric_range.step,
    )
    lines = [MetricLine(labels=dict(line.labels), points=line.points) for line in read.lines]
    return _envelope(
        series,
        stage,
        metric_range,
        now,
        read.state,
        read.reason,
        lines,
        complete=read.complete,
        covered_from=read.covered_from,
    )


def _resolve(name: str, range_name: str, scope: MetricScope, settings: Settings) -> tuple[MetricSeries, MetricRange]:
    series = find_series(name)
    if series is None or not series.in_scope(scope):
        raise HTTPException(status_code=404, detail=f"no {scope.value} series named {name!r}")
    metric_range = RANGES[range_name]
    if not series.allows(metric_range, settings.metrics_retention_days):
        # Refused rather than answered short: a chart of the retained part would look like the whole range.
        raise HTTPException(
            status_code=400,
            detail=f"range {range_name} is longer than the metrics retention of {settings.metrics_retention_days} days",
        )
    return series, metric_range


_RANGE = Query(alias="range", description="One of the catalog's ranges; there is no start, end or step parameter.")


def get_stage_metric(
    request: Request, stage: str, series: str, range_name: MetricRangeName = _RANGE
) -> MetricSeriesData:
    """One series' points for the stage. Blocks on Prometheus or Dagster, so FastAPI runs it on a worker thread."""
    settings: Settings = request.app.state.settings
    entry, metric_range = _resolve(series, range_name, MetricScope.STAGE, settings)
    now = datetime.now(UTC)
    if entry.source is MetricSource.DAGSTER:
        return _read_dagster(request, entry, stage, metric_range, now)
    return _read_prometheus(settings, entry, stage, metric_range, now, stage)


def get_platform_metric(request: Request, series: str, range_name: MetricRangeName = _RANGE) -> MetricSeriesData:
    """A platform series: the nodes and the olf-system components, which serve every stage."""
    settings: Settings = request.app.state.settings
    entry, metric_range = _resolve(series, range_name, MetricScope.PLATFORM, settings)
    return _read_prometheus(settings, entry, PLATFORM_STAGE, metric_range, datetime.now(UTC), None)


def _prometheus_states(settings: Settings, stage: str, now: datetime) -> dict[str, tuple[MetricState, str]]:
    """Each stage series' state from one `up` query: available while one of its targets is up now."""
    series = [entry for entry in catalog_for(MetricScope.STAGE) if entry.source is MetricSource.PROMETHEUS]
    client = _backend(settings)
    if isinstance(client, IntegrationStatus):
        return {entry.name: (client.status, client.reason) for entry in series}
    try:
        targets = client.query("up", time=now.timestamp())
    except ServiceClientError as exc:
        log.warning("the metrics backend could not be read for the catalog: %s", exc)
        return {entry.name: ("unavailable", "the metrics backend did not answer") for entry in series}
    states: dict[str, tuple[MetricState, str]] = {}
    for entry in series:
        matched = [
            target.samples[0][1]
            for target in targets
            if target.labels.get("job") in entry.jobs
            and (not entry.per_stage_targets or target.labels.get("stage") == stage)
        ]
        if any(matched):
            states[entry.name] = ("available", "a target behind the series is up")
        elif matched:
            states[entry.name] = ("unavailable", "every target behind the series is down")
        else:
            states[entry.name] = ("unavailable", "no target is scraped")
    return states


def _dagster_state(request: Request, stage: str) -> tuple[MetricState, str]:
    url = request.app.state.settings.dagster_graphql_urls.get(stage)
    if not url:
        return "disabled", "no Dagster is configured for this stage"
    try:
        DagsterClient(url).list_jobs()
    except ServiceClientError as exc:
        log.warning("Dagster for stage %s could not be read for the metrics catalog: %s", stage, exc)
        return "unavailable", "Dagster could not be read"
    return "available", "read from Dagster"


def list_stage_metrics(request: Request, stage: str) -> MetricCatalog:
    """The stage's catalog: each series, the ranges it serves here, and whether its source answers now."""
    settings: Settings = request.app.state.settings
    now = datetime.now(UTC)
    states = _prometheus_states(settings, stage, now)
    dagster = _dagster_state(request, stage)
    return MetricCatalog(
        stage=stage,
        observed_at=now,
        series=[
            MetricSeriesInfo(
                name=entry.name,
                unit=entry.unit,
                source=entry.source.value,
                labels=list(entry.labels),
                ranges=[
                    cast(MetricRangeName, name)
                    for name, r in RANGES.items()
                    if entry.allows(r, settings.metrics_retention_days)
                ],
                state=state,
                reason=reason,
            )
            for entry in catalog_for(MetricScope.STAGE)
            for state, reason in [dagster if entry.source is MetricSource.DAGSTER else states[entry.name]]
        ],
    )
