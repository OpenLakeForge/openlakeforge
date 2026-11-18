"""The metrics catalog: the allowlist of series and ranges the platform API serves (ADR 0024, decisions 3 and 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from openlakeforge_core.metrics import (
    CATALOG,
    RANGES,
    MetricScope,
    MetricSource,
    StepAggregation,
    catalog_for,
    find_series,
)

# Decision 3's label bound; `job` and `status` are the Dagster-derived series' own (decision 2), never Prometheus's.
PROMETHEUS_LABELS = {"stage", "component", "service", "domain"}
DAGSTER_LABELS = {"stage", "job", "status"}


def test_each_range_has_a_whole_number_of_steps_and_at_most_360_points() -> None:
    assert {name: (r.points, r.step) for name, r in RANGES.items()} == {
        "1h": (60, timedelta(minutes=1)),
        "6h": (360, timedelta(minutes=1)),
        "24h": (288, timedelta(minutes=5)),
        "7d": (336, timedelta(minutes=30)),
    }
    assert all(r.duration % r.step == timedelta(0) for r in RANGES.values())


def test_series_names_are_unique_and_found_by_name() -> None:
    assert len({series.name for series in CATALOG}) == len(CATALOG)
    assert find_series("trino_failed_queries") is not None
    assert find_series("up") is None


def test_no_served_series_carries_a_label_outside_the_bound() -> None:
    for series in CATALOG:
        allowed = PROMETHEUS_LABELS if series.source is MetricSource.PROMETHEUS else DAGSTER_LABELS
        assert set(series.labels) <= allowed, series.name


def test_a_prometheus_series_reads_only_its_own_recording_rule_and_names_the_jobs_that_vouch_for_it() -> None:
    for series in CATALOG:
        if series.source is MetricSource.PROMETHEUS:
            assert series.rule == f"olf:{series.name}"
            assert series.jobs, series.name
        else:
            assert series.rule is None and not series.jobs


def test_pipeline_runs_keep_their_status_label() -> None:
    runs = find_series("pipeline_runs")

    assert runs is not None and runs.labels == ("stage", "job", "status")


def test_the_platform_scope_has_no_stage_only_series_and_the_stage_scope_no_node_series() -> None:
    platform = {series.name for series in catalog_for(MetricScope.PLATFORM)}
    stage = {series.name for series in catalog_for(MetricScope.STAGE)}

    assert {"node_cpu_usage_cores", "object_store_disk_free_bytes"} <= platform - stage
    assert {"trino_failed_queries", "pipeline_runs", "pipeline_run_duration_seconds"} <= stage - platform
    assert "cpu_usage_cores" in platform & stage


@pytest.mark.parametrize(
    ("retention_days", "allowed"), [(1, {"1h", "6h", "24h"}), (6, {"1h", "6h", "24h"}), (7, set(RANGES))]
)
def test_a_range_past_retention_is_refused_for_prometheus_and_never_for_dagster(
    retention_days: int, allowed: set[str]
) -> None:
    cpu, runs = find_series("cpu_usage_cores"), find_series("pipeline_runs")
    assert cpu is not None and runs is not None

    assert {name for name, r in RANGES.items() if cpu.allows(r, retention_days)} == allowed
    assert all(runs.allows(r, retention_days) for r in RANGES.values())


def test_only_the_two_count_series_are_aggregated_per_step_each_as_its_rule_records_it() -> None:
    aggregated = {
        series.name: series.aggregation for series in CATALOG if series.aggregation is not StepAggregation.NONE
    }

    # M-c records both as per-minute increments, not counters, so increase() over them would be wrong.
    assert aggregated == {
        "container_restarts": StepAggregation.SUM_OVER_TIME,
        "trino_failed_queries": StepAggregation.SUM_OVER_TIME,
    }
