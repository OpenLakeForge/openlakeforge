"""The named metric series the platform API serves, and the ranges a chart may ask for (ADR 0024, decision 4).

A fixed allowlist: no caller names a query, only a series from `CATALOG` and a range from `RANGES`. Each Prometheus
series reads one `olf:` recording rule, whose labels are a subset of decision 3's four; the raw scraped series behind
the rules are never served. Nothing here performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class MetricScope(StrEnum):
    # A stage's own share; `platform` is the nodes and the olf-system components, which serve every stage.
    STAGE = "stage"
    PLATFORM = "platform"


class MetricSource(StrEnum):
    PROMETHEUS = "prometheus"
    # Dagster has no Prometheus endpoint at the pinned version, so run series are derived from its run storage
    # (ADR 0024, decision 2) and exist on Slim, where no Prometheus runs.
    DAGSTER = "dagster"


class StepAggregation(StrEnum):
    """How a recording rule is turned into one value per step."""

    # A gauge: the rule's value at each step.
    NONE = "none"
    # A counter: its increase over each step rather than its running total.
    INCREASE = "increase"
    # Per-interval increments the rule already computed: their sum over each step. M-c records `olf:container_restarts`
    # and `olf:trino_failed_queries` as per-minute counts, because a counter summed across pods drops when a pod goes,
    # and a rule recorded once a minute holds too few samples for increase() over a 60 s step.
    SUM_OVER_TIME = "sum_over_time"


@dataclass(frozen=True)
class MetricRange:
    name: str
    duration: timedelta
    step: timedelta

    @property
    def points(self) -> int:
        return self.duration // self.step


# No start, end or step parameter exists: a chart picks one of these, which bounds every read to at most 360 points.
RANGES: dict[str, MetricRange] = {
    r.name: r
    for r in (
        MetricRange("1h", timedelta(hours=1), timedelta(minutes=1)),
        MetricRange("6h", timedelta(hours=6), timedelta(minutes=1)),
        MetricRange("24h", timedelta(hours=24), timedelta(minutes=5)),
        MetricRange("7d", timedelta(days=7), timedelta(minutes=30)),
    )
}


@dataclass(frozen=True)
class MetricSeries:
    name: str
    unit: str
    scopes: frozenset[MetricScope]
    # The labels a line of this series may carry; anything else is dropped before it is served.
    labels: tuple[str, ...]
    source: MetricSource
    # The scrape jobs whose targets the series is read from; `up == 0` on all of them over a whole range is
    # `unavailable`, never an empty chart that reads as zero. Empty for a series Prometheus does not hold.
    jobs: tuple[str, ...] = ()
    # True where each stage has its own target behind the series (each stage's Trino coordinator), so the stage's
    # `up` alone vouches for it; the kubelet and object-store targets serve every stage at once.
    per_stage_targets: bool = False
    aggregation: StepAggregation = StepAggregation.NONE

    @property
    def rule(self) -> str | None:
        """The recording rule a Prometheus series reads; slice M-c's rule file defines one per catalog entry."""
        return f"olf:{self.name}" if self.source is MetricSource.PROMETHEUS else None

    def in_scope(self, scope: MetricScope) -> bool:
        return scope in self.scopes

    def allows(self, metric_range: MetricRange, retention_days: int) -> bool:
        """Whether `metric_range` lies within the backend's retention. A longer range is refused, never answered short.

        Dagster's run storage is not bounded by the metrics retention, so a Dagster series takes every range.
        """
        return self.source is MetricSource.DAGSTER or metric_range.duration <= timedelta(days=retention_days)


_STAGE = frozenset({MetricScope.STAGE})
_PLATFORM = frozenset({MetricScope.PLATFORM})
_BOTH = _STAGE | _PLATFORM
_PROMETHEUS = MetricSource.PROMETHEUS

CATALOG: tuple[MetricSeries, ...] = (
    MetricSeries("cpu_usage_cores", "cores", _BOTH, ("stage", "component"), _PROMETHEUS, ("kubelet-resource",)),
    MetricSeries(
        "memory_working_set_bytes", "bytes", _BOTH, ("stage", "component"), _PROMETHEUS, ("kubelet-resource",)
    ),
    MetricSeries(
        "container_restarts",
        "restarts",
        _BOTH,
        ("stage", "component"),
        _PROMETHEUS,
        ("kube-state-metrics",),
        aggregation=StepAggregation.SUM_OVER_TIME,
    ),
    MetricSeries("node_cpu_usage_cores", "cores", _PLATFORM, (), _PROMETHEUS, ("kubelet-resource",)),
    MetricSeries("node_memory_working_set_bytes", "bytes", _PLATFORM, (), _PROMETHEUS, ("kubelet-resource",)),
    MetricSeries(
        "trino_running_queries", "queries", _STAGE, ("stage",), _PROMETHEUS, ("trino",), per_stage_targets=True
    ),
    MetricSeries(
        "trino_queued_queries", "queries", _STAGE, ("stage",), _PROMETHEUS, ("trino",), per_stage_targets=True
    ),
    MetricSeries(
        "trino_failed_queries",
        "queries",
        _STAGE,
        ("stage",),
        _PROMETHEUS,
        ("trino",),
        per_stage_targets=True,
        aggregation=StepAggregation.SUM_OVER_TIME,
    ),
    MetricSeries("trino_heap_used_bytes", "bytes", _STAGE, ("stage",), _PROMETHEUS, ("trino",), per_stage_targets=True),
    MetricSeries("object_store_bucket_bytes", "bytes", _BOTH, ("stage",), _PROMETHEUS, ("object-store",)),
    MetricSeries("object_store_disk_used_bytes", "bytes", _PLATFORM, ("service",), _PROMETHEUS, ("object-store",)),
    MetricSeries("object_store_disk_free_bytes", "bytes", _PLATFORM, ("service",), _PROMETHEUS, ("object-store",)),
    # A line per job and run status, as the runs route reports them; per job, as pipeline health is.
    MetricSeries("pipeline_runs", "runs", _STAGE, ("stage", "job", "status"), MetricSource.DAGSTER),
    MetricSeries("pipeline_run_duration_seconds", "seconds", _STAGE, ("stage", "job"), MetricSource.DAGSTER),
)

_BY_NAME = {series.name: series for series in CATALOG}


def find_series(name: str) -> MetricSeries | None:
    return _BY_NAME.get(name)


def catalog_for(scope: MetricScope) -> tuple[MetricSeries, ...]:
    return tuple(series for series in CATALOG if series.in_scope(scope))
