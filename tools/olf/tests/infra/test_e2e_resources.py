from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import e2e_cfg

from olf.infra.e2e import _resources
from olf.infra.e2e._shell import E2EError

_BUDGET = {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}


def _workload(kind: str, resources: dict[str, Any], *, init: bool = False) -> dict[str, Any]:
    spec = {"initContainers" if init else "containers": [{"name": "worker", "resources": resources}]}
    if kind != "Pod":
        spec = {"template": {"spec": spec}}
    if kind == "CronJob":
        spec = {"jobTemplate": {"spec": spec}}
    return {"kind": kind, "metadata": {"name": "work"}, "spec": spec}


@pytest.mark.parametrize("kind", ["Pod", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"])
@pytest.mark.parametrize("init", [False, True])
def test_all_workload_templates_accept_a_budget_without_a_cpu_limit(kind: str, init: bool) -> None:
    assert _resources.workload_resource_failures({"items": [_workload(kind, _BUDGET, init=init)]}) == []


@pytest.mark.parametrize("group,key", [("requests", "cpu"), ("requests", "memory"), ("limits", "memory")])
@pytest.mark.parametrize("value", [None, "0", "0m", "0Mi"])
def test_missing_and_zero_budgets_report_the_workload_container_and_field(
    group: str,
    key: str,
    value: str | None,
) -> None:
    budget = copy.deepcopy(_BUDGET)
    if value is None:
        del budget[group][key]
    else:
        budget[group][key] = value
    problems = _resources.workload_resource_failures({"items": [_workload("Job", budget)]})
    assert problems == [f"Job/work container worker: missing positive {group}.{key}"]


def test_conformance_collects_both_stage_and_shared_workloads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = replace(e2e_cfg(tmp_path, env="aws"), namespace="olf-dev", shared_namespace="olf-system")
    namespaces = []

    def kubectl(_cfg, args, *, capture):
        namespace = args[args.index("-n") + 1]
        namespaces.append(namespace)
        return json.dumps({"items": [_workload("Deployment", _BUDGET)]})

    monkeypatch.setattr(_resources, "kubectl", kubectl)
    _resources.check_workload_resources(cfg)
    assert namespaces == ["olf-system", "olf-dev"]


@pytest.mark.parametrize("items", [[], [_workload("Pod", {})]])
def test_empty_or_unbudgeted_deployments_fail_conformance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    items: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(_resources, "kubectl", lambda *_args, **_kwargs: json.dumps({"items": items}))
    with pytest.raises(E2EError, match="workload resource conformance failed"):
        _resources.check_workload_resources(e2e_cfg(tmp_path, env="aws"))


def test_storage_requires_existing_class_and_bound_sized_volume() -> None:
    claim = {
        "metadata": {"name": "data", "namespace": "olf-system"},
        "spec": {"storageClassName": "standard", "volumeName": "pv1", "resources": {"requests": {"storage": "5Gi"}}},
        "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}},
    }
    classes = {"items": [{"metadata": {"name": "standard"}}]}
    volumes = {
        "items": [{"metadata": {"name": "pv1"}, "spec": {"claimRef": {"name": "data", "namespace": "olf-system"}}}]
    }
    assert _resources.storage_failures({"items": [claim]}, classes, volumes) == []
    claim["spec"]["storageClassName"] = "missing"
    claim["status"]["capacity"]["storage"] = "0Gi"
    assert _resources.storage_failures({"items": [claim]}, classes, {"items": []}) == [
        "PVC/data: storage class 'missing' does not exist",
        "PVC/data: not bound to its persistent volume",
        "PVC/data: missing positive storage capacity",
    ]


def test_disruption_requires_budget_for_replicas_and_allows_single_pod_eviction() -> None:
    workload = {
        "kind": "Deployment",
        "metadata": {"name": "service"},
        "spec": {
            "replicas": 2,
            "template": {"metadata": {"labels": {"app": "service"}}},
        },
    }
    budget = {
        "metadata": {"name": "service-pdb"},
        "spec": {"selector": {"matchLabels": {"app": "service"}}},
        "status": {"disruptionsAllowed": 1},
    }
    assert _resources.disruption_failures({"items": [workload]}, {"items": []}) == [
        "Deployment/service: replicated workload has no disruption budget"
    ]
    assert _resources.disruption_failures({"items": [workload]}, {"items": [budget]}) == []
    workload["spec"]["replicas"] = 1
    budget["status"]["disruptionsAllowed"] = 0
    assert _resources.disruption_failures({"items": [workload]}, {"items": [budget]}) == [
        "Deployment/service: disruption budget service-pdb blocks eviction"
    ]


def test_local_conformance_checks_storage_and_disruption(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def kubectl(_cfg, args, *, capture):
        kind = args[1]
        if kind == "storageclasses":
            return json.dumps({"items": [{"metadata": {"name": "standard"}}]})
        if kind == "persistentvolumes":
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "pv"},
                            "spec": {
                                "claimRef": {
                                    "name": "data",
                                    "namespace": "lakehouse",
                                }
                            },
                        }
                    ]
                }
            )
        if kind == "persistentvolumeclaims":
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "data", "namespace": "lakehouse"},
                            "spec": {
                                "storageClassName": "standard",
                                "volumeName": "pv",
                                "resources": {"requests": {"storage": "1Gi"}},
                            },
                            "status": {"phase": "Bound", "capacity": {"storage": "1Gi"}},
                        }
                    ]
                }
            )
        if kind == "poddisruptionbudgets":
            return json.dumps({"items": []})
        return json.dumps({"items": [_workload("Deployment", _BUDGET)]})

    monkeypatch.setattr(_resources, "kubectl", kubectl)
    _resources.check_workload_resources(e2e_cfg(tmp_path))
