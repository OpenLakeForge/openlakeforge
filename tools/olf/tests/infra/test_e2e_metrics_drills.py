from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from openlakeforge_core.metrics import MetricScope, MetricSource, catalog_for

from olf.infra.e2e import _metrics_drills as drills
from olf.infra.e2e._log_footprint import Usage
from olf.infra.e2e._shell import E2EError

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_only_an_unavailable_answer_with_no_lines_passes_the_outage() -> None:
    assert drills.outage_problem({"state": "unavailable", "reason": "down", "lines": []}) is None
    assert "available" in str(drills.outage_problem({"state": "available", "reason": "ok", "lines": []}))
    assert "carries lines" in str(drills.outage_problem({"state": "unavailable", "lines": [{"points": []}]}))


def test_the_catalog_during_an_outage_follows_each_source() -> None:
    catalog = {
        "series": [
            {"name": "cpu_usage_cores", "source": "prometheus", "state": "unavailable"},
            {"name": "trino_heap_used_bytes", "source": "prometheus", "state": "available"},
            {"name": "pipeline_runs", "source": "dagster", "state": "available"},
            {"name": "pipeline_run_duration_seconds", "source": "dagster", "state": "unavailable"},
        ]
    }
    assert drills.catalog_outage_problems(catalog) == [
        "trino_heap_used_bytes is listed 'available' with Prometheus down",
        "pipeline_run_duration_seconds is listed 'unavailable'; Dagster still answers",
    ]


def test_the_metrics_integration_is_read_by_name() -> None:
    health = {"integrations": [{"name": "dagster", "status": "available"}, {"name": "metrics", "status": "disabled"}]}
    assert drills.integration(health) == "disabled"
    assert drills.integration({"integrations": []}) is None


def test_the_outage_reads_every_prometheus_series_of_both_scopes_and_no_dagster_one() -> None:
    paths = drills.prometheus_series_paths("dev")
    prometheus = {s.name for s in catalog_for(MetricScope.STAGE) if s.source is MetricSource.PROMETHEUS}
    assert {k.removeprefix("stage ") for k in paths if k.startswith("stage ")} == prometheus
    assert {k.removeprefix("platform ") for k in paths if k.startswith("platform ")} == {
        s.name for s in catalog_for(MetricScope.PLATFORM)
    }
    assert paths["stage cpu_usage_cores"] == "/api/v1/stages/dev/metrics/cpu_usage_cores?range=1h"


def _pod(memory_limit: str | None = "768Mi") -> dict[str, Any]:
    resources: dict[str, Any] = {"requests": {"cpu": "100m", "memory": "256Mi"}}
    if memory_limit:
        resources["limits"] = {"cpu": "500m", "memory": memory_limit}
    return {"spec": {"containers": [{"name": "prometheus", "resources": resources}]}}


def test_a_workload_footprint_is_its_mean_and_busiest_cpu_and_peak_memory() -> None:
    samples = [
        (0.0, {"prometheus": Usage(0, 100 * 1024**2, 0)}),
        (10.0, {"prometheus": Usage(1_000_000_000, 150 * 1024**2, 0)}),
        (20.0, {"prometheus": Usage(1_200_000_000, 120 * 1024**2, 0)}),
    ]
    footprint = drills.summarize("prometheus", samples, _pod())
    assert footprint.cpu_millicores_mean == pytest.approx(60.0)
    assert footprint.cpu_millicores_peak == pytest.approx(100.0)
    assert footprint.working_set_peak_bytes == 150 * 1024**2
    assert (footprint.memory_request_bytes, footprint.memory_limit_bytes) == (256 * 1024**2, 768 * 1024**2)
    assert drills.summarize("prometheus", samples, _pod(None)).memory_limit_bytes is None
    with pytest.raises(E2EError, match="fewer than two"):
        drills.summarize("prometheus", samples[:1], _pod())


def test_volume_use_comes_from_the_claims_stats_or_is_unreported() -> None:
    summary = {
        "pods": [
            {
                "podRef": {"name": "prometheus-abc", "namespace": "olf-system"},
                "volume": [{"name": "data", "pvcRef": {"name": "prometheus"}, "usedBytes": 4096}],
            }
        ]
    }
    assert drills.volume_used_bytes(summary, "olf-system", "prometheus-abc", "prometheus") == 4096
    assert drills.volume_used_bytes(summary, "olf-system", "prometheus-abc", "other") is None
    assert drills.volume_used_bytes({"pods": []}, "olf-system", "prometheus-abc", "prometheus") is None


def test_the_deployed_size_is_read_from_the_metrics_volume_in_the_sizes_file() -> None:
    sizes = yaml.safe_load((REPO_ROOT / "release/storage-sizes.yaml").read_text())["sizes"]
    assert drills.size_for_volume(sizes, "4Gi") == "small"
    assert drills.size_for_volume(sizes, "16Gi") == "large"
    assert drills.size_for_volume(sizes, "5Gi") is None


def test_the_footprint_table_says_when_the_volume_reports_nothing() -> None:
    footprint = drills.WorkloadFootprint("prometheus", 12.0, 40.0, 200 * 1024**2, None, 768 * 1024**2, None, "500m")
    record = {
        "size": "small",
        "volume": "4Gi",
        "volume_used_bytes": None,
        "prometheus_age_seconds": 3600,
        "head_series": 2500.0,
    }
    table = drills.render_footprint([footprint], record)
    assert "| prometheus | 12 | 40 | 200 | n/a / 768 |" in table
    assert "not reported used after 60 minutes" in table
    assert "Head series: 2500 (budget 10000)" in table
