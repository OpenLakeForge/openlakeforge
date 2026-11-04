"""Resource-budget conformance for deployed pods and workload templates."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from olf.common import log
from olf.infra.e2e._health import health_namespaces
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl

_ZERO = re.compile(r"0+(?:\.0+)?(?:[a-zA-Z]+)?$")


def workload_resource_failures(payload: Mapping[str, Any]) -> list[str]:
    problems = []
    for workload in payload.get("items", []):
        kind = workload.get("kind", "Pod")
        spec = workload.get("spec", {})
        if kind == "CronJob":
            spec = spec.get("jobTemplate", {}).get("spec", {})
        if kind != "Pod":
            spec = spec.get("template", {}).get("spec", {})
        identity = f"{kind}/{workload['metadata']['name']}"
        for container in [*spec.get("initContainers", []), *spec.get("containers", [])]:
            resources = container.get("resources", {})
            missing = [
                f"{group}.{resource}"
                for group, resource in (("requests", "cpu"), ("requests", "memory"), ("limits", "memory"))
                if not resources.get(group, {}).get(resource) or _ZERO.fullmatch(str(resources[group][resource]))
            ]
            if missing:
                problems.append(f"{identity} container {container['name']}: missing positive {', '.join(missing)}")
    return problems


def storage_failures(claims: Mapping[str, Any], classes: Mapping[str, Any], volumes: Mapping[str, Any]) -> list[str]:
    known_classes = {item["metadata"]["name"] for item in classes.get("items", [])}
    known_volumes = {item["metadata"]["name"]: item for item in volumes.get("items", [])}
    problems = []
    for claim in claims.get("items", []):
        name = claim["metadata"]["name"]
        spec, status = claim.get("spec", {}), claim.get("status", {})
        storage_class = spec.get("storageClassName")
        if not storage_class or storage_class not in known_classes:
            problems.append(f"PVC/{name}: storage class {storage_class!r} does not exist")
        volume = known_volumes.get(spec.get("volumeName"), {})
        reference = volume.get("spec", {}).get("claimRef", {})
        if (
            status.get("phase") != "Bound"
            or reference.get("name") != name
            or reference.get("namespace") != claim["metadata"].get("namespace")
        ):
            problems.append(f"PVC/{name}: not bound to its persistent volume")
        for label, quantity in (
            ("request", spec.get("resources", {}).get("requests", {}).get("storage")),
            ("capacity", status.get("capacity", {}).get("storage")),
        ):
            if not quantity or _ZERO.fullmatch(str(quantity)):
                problems.append(f"PVC/{name}: missing positive storage {label}")
    return problems


def disruption_failures(workloads: Mapping[str, Any], budgets: Mapping[str, Any]) -> list[str]:
    problems = []
    for workload in workloads.get("items", []):
        kind = workload.get("kind")
        if kind not in ("Deployment", "StatefulSet", "DaemonSet"):
            continue
        spec = workload.get("spec", {})
        replicas = (
            workload.get("status", {}).get("desiredNumberScheduled", 0)
            if kind == "DaemonSet"
            else spec.get("replicas", 1)
        )
        labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
        matches = []
        for budget in budgets.get("items", []):
            selector = budget.get("spec", {}).get("selector")
            if selector is None or any(labels.get(k) != v for k, v in selector.get("matchLabels", {}).items()):
                continue
            expressions = selector.get("matchExpressions", [])
            if not all(
                (expr["operator"] == "In" and labels.get(expr["key"]) in expr.get("values", []))
                or (expr["operator"] == "NotIn" and labels.get(expr["key"]) not in expr.get("values", []))
                or (expr["operator"] == "Exists" and expr["key"] in labels)
                or (expr["operator"] == "DoesNotExist" and expr["key"] not in labels)
                for expr in expressions
            ):
                continue
            matches.append(budget)
        identity = f"{kind}/{workload['metadata']['name']}"
        if replicas > 1 and not matches:
            problems.append(f"{identity}: replicated workload has no disruption budget")
        if replicas >= 1:
            for budget in matches:
                if budget.get("status", {}).get("disruptionsAllowed") == 0:
                    problems.append(f"{identity}: disruption budget {budget['metadata']['name']} blocks eviction")
    return problems


def check_workload_resources(cfg: E2EConfig) -> None:
    log.step("Checking workload resource budgets...")
    problems = []
    claims_found = 0
    if cfg.env == "local":
        classes = json.loads(kubectl(cfg, ["get", "storageclasses", "-o", "json"], capture=True))
        volumes = json.loads(kubectl(cfg, ["get", "persistentvolumes", "-o", "json"], capture=True))
    for namespace in health_namespaces(cfg):
        payload = json.loads(
            kubectl(
                cfg,
                ["get", "pods,deployments,statefulsets,daemonsets,jobs,cronjobs", "-n", namespace, "-o", "json"],
                capture=True,
            )
        )
        if not payload.get("items"):
            problems.append(f"{namespace}: no workloads found")
        problems.extend(f"{namespace}: {problem}" for problem in workload_resource_failures(payload))
        if cfg.env == "local":
            budgets = json.loads(
                kubectl(cfg, ["get", "poddisruptionbudgets", "-n", namespace, "-o", "json"], capture=True)
            )
            problems.extend(f"{namespace}: {problem}" for problem in disruption_failures(payload, budgets))
            claims = json.loads(
                kubectl(cfg, ["get", "persistentvolumeclaims", "-n", namespace, "-o", "json"], capture=True)
            )
            claims_found += len(claims.get("items", []))
            problems.extend(f"{namespace}: {problem}" for problem in storage_failures(claims, classes, volumes))
    if cfg.env == "local" and not claims_found:
        problems.append("no persistent volume claims found")
    if problems:
        raise E2EError("workload resource conformance failed:\n" + "\n".join(problems))
