from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import e2e_cfg
from openlakeforge_core.metrics import MetricScope, catalog_for

from olf.infra.e2e import _metrics
from olf.infra.e2e._shell import E2EError


def envelope(state: str, *values: float | None) -> dict[str, Any]:
    points = [[f"2026-11-19T10:0{i}:00Z", v] for i, v in enumerate(values)]
    return {"state": state, "reason": "r", "lines": [{"labels": {"stage": "dev"}, "points": points}] if values else []}


def test_the_reader_is_the_mapped_role_nearest_the_floor_that_the_stage_admits() -> None:
    precedence = ["platform-admin", "data-engineer", "analyst", "viewer"]
    mapping = {"platform-admin": ["o:a"], "data-engineer": ["o:d"], "viewer": ["o:v"]}
    assert _metrics.session_roles(precedence, mapping, "data-engineer") == ("data-engineer", "viewer")
    on_prod = _metrics.session_roles(precedence, mapping, "data-engineer", lambda role: role != "data-engineer")
    assert on_prod == ("platform-admin", "viewer")
    assert _metrics.session_roles(precedence, {"analyst": ["o:x"]}, "data-engineer") == (None, "analyst")


def test_a_series_counts_only_when_available_with_a_point_that_is_not_null() -> None:
    assert _metrics.has_points(envelope("available", None, 0.0))
    assert not _metrics.has_points(envelope("available", None, None))
    assert not _metrics.has_points(envelope("unavailable", 3.0))
    assert not _metrics.has_points(envelope("available"))


def test_a_source_is_missing_only_when_none_of_its_series_has_a_point() -> None:
    sources = {"kubelet": ("cpu_usage_cores", "memory_working_set_bytes"), "trino": ("trino_heap_used_bytes",)}
    read = {
        "cpu_usage_cores": envelope("unavailable"),
        "memory_working_set_bytes": envelope("available", 1.0),
        "trino_heap_used_bytes": envelope("available", None),
    }
    missing = _metrics.missing_sources(read, sources)
    assert list(missing) == ["trino"]
    assert missing["trino"] == ["trino_heap_used_bytes: available (r)"]


def test_every_series_a_source_names_is_in_the_catalog_of_its_scope() -> None:
    stage = {s.name for s in catalog_for(MetricScope.STAGE)}
    platform = {s.name for s in catalog_for(MetricScope.PLATFORM)}
    assert {n for names in _metrics.STAGE_SOURCES.values() for n in names} <= stage
    assert {n for names in _metrics.PLATFORM_SOURCES.values() for n in names} <= platform
    assert set(_metrics.stage_series_paths("dev")) == stage
    assert _metrics.platform_series_paths()["node_cpu_usage_cores"] == (
        "/api/v1/platform/metrics/node_cpu_usage_cores?range=6h"
    )


def test_a_metrics_refusal_matches_the_health_routes_with_its_own_operation() -> None:
    health = {"detail": "role 'analyst' may not perform 'get_stage_health'; it needs 'data-engineer' or above"}
    metric = {"detail": "role 'analyst' may not perform 'get_stage_metric'; it needs 'data-engineer' or above"}
    assert _metrics.same_refusal(health, "get_stage_health", metric, "get_stage_metric")
    assert not _metrics.same_refusal(health, "get_stage_health", metric, "list_stage_metrics")
    assert not _metrics.same_refusal(health, "get_stage_health", {"detail": "operation not permitted"}, "x")


def test_a_query_answer_parses_to_labels_and_values() -> None:
    body = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"__name__": "prometheus_tsdb_head_series"}, "value": [1.7e9, "4321"]}],
            },
        }
    )
    assert _metrics.parse_query(body) == [({"__name__": "prometheus_tsdb_head_series"}, 4321.0)]
    with pytest.raises(E2EError, match="vector"):
        _metrics.parse_query(json.dumps({"status": "error", "error": "bad query"}))


def test_a_series_answer_parses_to_label_sets() -> None:
    body = json.dumps({"status": "success", "data": [{"__name__": "olf:cpu_usage_cores", "stage": "dev"}]})
    assert _metrics.parse_series(body) == [{"__name__": "olf:cpu_usage_cores", "stage": "dev"}]
    with pytest.raises(E2EError):
        _metrics.parse_series(json.dumps({"status": "error"}))


def test_only_labels_outside_decision_3_are_violations() -> None:
    series = [
        {"__name__": "olf:cpu_usage_cores", "stage": "dev", "component": "trino"},
        {"__name__": "olf:object_store_disk_used_bytes", "service": "volume", "domain": "sales"},
        {"__name__": "olf:memory_working_set_bytes", "stage": "dev", "pod": "dagster-run-1234", "job": "kubelet"},
        {"__name__": "olf:node_cpu_usage_cores"},
    ]
    assert _metrics.label_violations(series) == [
        "olf:memory_working_set_bytes{job}",
        "olf:memory_working_set_bytes{pod}",
    ]


def test_the_budget_holds_strictly_under_its_value() -> None:
    assert _metrics.budget_problem(9_999) is None
    assert "10000 series budget" in str(_metrics.budget_problem(10_000))
    assert _metrics.budget_problem(500, budget=400) is not None


def test_metrics_objects_are_found_by_name_or_by_their_pods_name_label() -> None:
    items = [
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "prometheus"}},
        {"kind": "Pod", "metadata": {"name": "ksm-abc", "labels": {"app.kubernetes.io/name": "kube-state-metrics"}}},
        {"kind": "Service", "metadata": {"name": "trino", "labels": {"app.kubernetes.io/name": "trino"}}},
    ]
    assert _metrics.metric_objects(items) == ["PersistentVolumeClaim/prometheus", "Pod/ksm-abc"]


def test_a_policy_left_for_the_metrics_backend_is_found_by_name_or_by_a_peer_it_admits() -> None:
    def policy(name: str, spec: dict[str, Any]) -> dict[str, Any]:
        return {"metadata": {"name": name, "namespace": "olf-system"}, "spec": spec}

    api_egress = {
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "platform-api"}},
        "egress": [{"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}}}]}],
    }
    policies = [
        policy("olf-flows-kube-state-metrics", {}),
        policy("olf-flows-platform-api", api_egress),
        policy("olf-flows-trino", {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "trino"}}}),
    ]
    assert _metrics.metric_policies(policies) == [
        "olf-system/olf-flows-kube-state-metrics",
        "olf-system/olf-flows-platform-api",
    ]


def _contract(**metrics: Any) -> dict[str, Any]:
    return {"shared": {"observability": {"metrics": metrics}}}


def test_metrics_are_served_only_when_enabled_supported_and_reachable() -> None:
    assert _metrics.metrics_on({"enabled": True, "supported": True, "query_url": "http://prometheus:9090"})
    assert not _metrics.metrics_on({"enabled": True, "supported": False, "query_url": None})
    assert not _metrics.metrics_on({"enabled": False, "supported": True, "query_url": None})


def test_metrics_off_fails_on_a_leftover_and_does_nothing_when_metrics_are_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = e2e_cfg(tmp_path)
    answers = {
        "deployments,pods,persistentvolumeclaims,services,configmaps,serviceaccounts": {
            "items": [{"kind": "Service", "metadata": {"name": "prometheus"}}]
        },
        "networkpolicy": {"items": []},
    }

    def kubectl(_cfg: object, args: list[str], **_: object) -> str:
        if args[1] == "clusterrole":
            return ""
        return json.dumps(answers[args[1]])

    monkeypatch.setattr(_metrics, "kubectl", kubectl)
    monkeypatch.setattr(_metrics, "load_provider_contracts_or_raise", lambda _cfg: _contract(enabled=False))
    with pytest.raises(E2EError, match="Service/prometheus"):
        _metrics.check_metrics_off(cfg)

    monkeypatch.setattr(_metrics, "load_provider_contracts_or_raise", lambda _cfg: _contract(enabled=True))
    _metrics.check_metrics_off(cfg)
