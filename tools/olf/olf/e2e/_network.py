"""NetworkPolicy and RBAC conformance (#46, ADR 0017 "Conformance test").

Positive: every flow row tagged T whose two ends are pods (or the API server)
connects, from a probe pod carrying the source's namespace and labels.
Negative: a pod with no allowed flow cannot reach Trino, Polaris, PostgreSQL,
OpenMetadata or Dex. RBAC: `kubectl auth can-i --as=<ServiceAccount>` is denied
operations the ADR does not document.

Known gap, not fixed here: under kindnet a new pod's policy applies a moment
after it is Ready, so a fresh pod can briefly reach what it should not. The
probes wait `SETTLE_SECONDS` before connecting; the untrusted-namespace
negatives do not depend on it, because the targets' ingress policy already
exists.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from typing import Any

import yaml

from olf import k8s, log
from olf.e2e._shell import E2EConfig, E2EError, kubectl

FLOWS_FILE = "release/network-flows.yaml"
UNTRUSTED_NAMESPACE = "olf-e2e-untrusted"
SETTLE_SECONDS = 5
CONNECT_TIMEOUT = 5
# curl: 6 no DNS, 7 refused, 28 timed out. Only a timeout proves a drop; 6 and 7 prove a broken probe.
NOT_REACHED = {6, 7, 28}
# Targets of the negatives: the protected backends (workload name in the flow file).
PROTECTED = ("postgresql", "polaris", "trino", "openmetadata", "dex")
# ADR 0017 exceptions E4 and E5 read Secrets cluster-wide by design.
CLUSTER_SECRET_READERS = ("traefik", "cert-manager")
# Roles that may grant Secret reads without resourceNames or create workloads: E2 to E5.
SECRET_VERBS = {"get", "list", "watch", "update", "patch", "delete", "*"}
WORKLOADS = {"pods", "jobs", "cronjobs", "deployments", "daemonsets", "statefulsets", "*"}
DOCUMENTED_ROLES = ("openmetadata", "traefik", "cert-manager", "dagster")


def _selector(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def load_flows(cfg: E2EConfig) -> dict[str, Any]:
    return yaml.safe_load((cfg.distribution_root / FLOWS_FILE).read_text())


def positive_probes(document: dict[str, Any]) -> list[tuple[str, dict[str, str], dict[str, Any], int]]:
    """(source scope, source labels, destination endpoint, port) for each T row between pods or to the API server.

    Rows whose ends are outside the cluster, a node or a whole stage namespace
    cannot be probed from one pod and are left to the full e2e run.
    """
    probes = []
    for row in document["flows"]:
        destination = row["to"]
        if row["tag"] != "T" or row.get("protocols", ["TCP"]) != ["TCP"]:
            continue
        if destination["scope"] not in {"shared", "apiserver"} or (
            destination["scope"] == "shared" and not destination.get("pods")
        ):
            continue
        sources = row["from"] if isinstance(row["from"], list) else [row["from"]]
        for source in sources:
            if source["scope"] in {"stage", "shared"} and (source["scope"] == "stage" or source.get("pods")):
                for port in row["ports"]:
                    probes.append((source["scope"], source.get("pods", {}), destination, port))
    return probes


def workload_port(document: dict[str, Any], workload: str) -> int:
    labels = document["workloads"][workload]
    for row in document["flows"]:
        if row["to"].get("pods") == labels:
            return int(row["ports"][0])
    raise E2EError(f"no flow row reaches workload {workload}")


def _pod_ip(cfg: E2EConfig, namespace: str, labels: dict[str, str]) -> str | None:
    out = kubectl(
        cfg,
        ["get", "pod", "-n", namespace, "-l", _selector(labels), "--field-selector=status.phase=Running",
         "-o", "jsonpath={.items[0].status.podIP}"],
        capture=True,
    ).strip()
    return out or None


def _apiserver(cfg: E2EConfig) -> str:
    """The API server endpoint address; a refusal (rc 7) on the port not served still proves the policy allowed it."""
    get = ["get", "endpoints", "kubernetes", "-n", "default", "-o", "json"]
    endpoints = json.loads(kubectl(cfg, get, capture=True))
    return str(endpoints["subsets"][0]["addresses"][0]["ip"])


@contextlib.contextmanager
def _probe_pod(cfg: E2EConfig, namespace: str, labels: dict[str, str], name: str) -> Iterator[str]:
    image = yaml.safe_load((cfg.distribution_root / "release/component-catalog.yaml").read_text())[
        "components"
    ]["images"]["k8s_bootstrap"]
    run = ["run", name, "-n", namespace, "--restart=Never", f"--image={image}"]
    if labels:
        run.append(f"--labels={_selector(labels)}")
    kubectl(cfg, [*run, "--", "sleep", "600"])
    try:
        kubectl(cfg, ["wait", "--for=condition=Ready", f"pod/{name}", "-n", namespace, "--timeout=120s"])
        time.sleep(SETTLE_SECONDS)
        yield name
    finally:
        kubectl(cfg, ["delete", f"pod/{name}", "-n", namespace, "--ignore-not-found", "--wait=false"])


def _connect(cfg: E2EConfig, namespace: str, pod: str, ip: str, port: int) -> int:
    """curl's exit code for a TCP connect to ip:port (any HTTP answer is 0, a drop is 28)."""
    script = f"curl -s -o /dev/null --connect-timeout {CONNECT_TIMEOUT} http://{ip}:{port}/; echo rc=$?"
    out = kubectl(cfg, ["exec", "-n", namespace, pod, "--", "sh", "-c", script], capture=True)
    return int(out.rsplit("rc=", 1)[1])


def check_positive_flows(cfg: E2EConfig, document: dict[str, Any]) -> None:
    log.step("Checking that the T flows connect with default-deny on...")
    sources: dict[tuple[str, str], list[tuple[Any, int]]] = {}
    for scope, labels, destination, port in positive_probes(document):
        sources.setdefault((scope, _selector(labels)), []).append((destination, port))
    failures: list[str] = []
    for index, ((scope, selector), targets) in enumerate(sorted(sources.items())):
        labels = dict(pair.split("=", 1) for pair in selector.split(",") if pair)
        namespace = cfg.namespace if scope == "stage" else cfg.platform_namespace
        with _probe_pod(cfg, namespace, labels, f"olf-e2e-flow-{index}") as pod:
            for destination, port in targets:
                ip = (
                    _apiserver(cfg)
                    if destination["scope"] == "apiserver"
                    else _pod_ip(cfg, cfg.platform_namespace, destination["pods"])
                )
                if ip is None:
                    log.info(f"Skipping {scope}[{selector}] -> {destination}:{port}: no such pod or endpoint.")
                    continue
                if _connect(cfg, namespace, pod, ip, port) == 28:
                    failures.append(f"{namespace}[{selector or 'no labels'}] -> {ip}:{port} {destination} timed out")
    if failures:
        raise E2EError("a documented flow is blocked:\n" + "\n".join(failures))


def check_negative_flows(cfg: E2EConfig, document: dict[str, Any]) -> None:
    log.step("Checking that pods with no allowed flow cannot reach the protected backends...")
    targets = {}
    for workload in PROTECTED:
        ip = _pod_ip(cfg, cfg.platform_namespace, document["workloads"][workload])
        if ip is None:
            log.info(f"Skipping {workload}: not deployed.")
            continue
        targets[workload] = (ip, workload_port(document, workload))
    if not targets:
        raise E2EError("no protected backend is running, so the negative flows prove nothing")
    kubectl(cfg, ["create", "namespace", UNTRUSTED_NAMESPACE])
    problems: list[str] = []
    try:
        # (namespace, name, allowed): the probes are unlabelled, so only F6 (stage to PostgreSQL) applies.
        cases = [
            (UNTRUSTED_NAMESPACE, "outside", set()),
            (cfg.namespace, "stage", {"postgresql"}),
            (cfg.platform_namespace, "platform", set()),
        ]
        for namespace, name, allowed in cases:
            with _probe_pod(cfg, namespace, {}, f"olf-e2e-deny-{name}") as pod:
                for workload, (ip, port) in targets.items():
                    rc = _connect(cfg, namespace, pod, ip, port)
                    if workload in allowed:
                        if rc in NOT_REACHED:  # a control: a broken probe cannot pass vacuously
                            problems.append(f"control {namespace} -> {workload}:{port} did not connect (rc={rc})")
                    elif rc != 28:
                        problems.append(f"{namespace} -> {workload} {ip}:{port} was not dropped (rc={rc})")
    finally:
        kubectl(cfg, ["delete", "namespace", UNTRUSTED_NAMESPACE, "--ignore-not-found", "--wait=false"])
    if problems:
        raise E2EError("network isolation breach:\n" + "\n".join(problems))


def _can_i(cfg: E2EConfig, subject: str, *args: str) -> bool:
    # `can-i` exits 1 on "no"; the answer, not the exit code, is the result.
    argv = ["auth", "can-i", f"--as={subject}", *args]
    out = k8s._kubectl(argv, capture=True, check=False, kube_context=cfg.kube_context)
    return out.strip() == "yes"


def _service_accounts(cfg: E2EConfig) -> list[tuple[str, str]]:
    found = []
    for namespace in dict.fromkeys((cfg.platform_namespace, cfg.namespace)):
        get = ["get", "serviceaccount", "-n", namespace, "-o", "json"]
        items = json.loads(kubectl(cfg, get, capture=True))["items"]
        found += [(namespace, item["metadata"]["name"]) for item in items]
    return found


def rbac_denials(namespace: str, name: str) -> list[tuple[str, ...]]:
    """can-i arguments that must answer no for this ServiceAccount (ADR 0017, "Denied")."""
    denied: list[tuple[str, ...]] = [
        ("escalate", "clusterroles"),
        ("bind", "roles", "-n", namespace),
        ("patch", "configmap/coredns", "-n", "kube-system"),
    ]
    if not name.startswith(CLUSTER_SECRET_READERS):
        denied += [
            ("list", "secrets", "--all-namespaces"),
            ("get", "secrets", "-n", "kube-system"),
            ("create", "serviceaccounts/token", "-n", namespace),
        ]
    if name == "default":
        denied += [
            ("get", "secrets", "-n", namespace),
            ("list", "secrets", "-n", namespace),
            ("create", "pods", "-n", namespace),
            ("create", "rolebindings", "-n", namespace),
        ]
    return denied


def check_rbac(cfg: E2EConfig) -> None:
    log.step("Checking ServiceAccount permissions against the documented operations...")
    problems: list[str] = []
    accounts = _service_accounts(cfg)
    for namespace, name in accounts:
        subject = f"system:serviceaccount:{namespace}:{name}"
        problems += [
            f"{subject} may {' '.join(args)}" for args in rbac_denials(namespace, name) if _can_i(cfg, subject, *args)
        ]
    # Controls: the Jobs keep what the ADR documents, and an unlisted Secret stays out of reach.
    bootstrap = f"system:serviceaccount:{cfg.platform_namespace}:polaris-bootstrap"
    if (cfg.platform_namespace, "polaris-bootstrap") in accounts:
        if not _can_i(cfg, bootstrap, "create", "secrets", "-n", cfg.platform_namespace):
            problems.append("control: polaris-bootstrap lost create on Secrets")
        if _can_i(cfg, bootstrap, "get", "secret/postgresql", "-n", cfg.platform_namespace):
            problems.append("polaris-bootstrap may read Secret postgresql, which no flow of its own names")
    get = ["get", "clusterrole", "seaweedfs-rw-cr", "--ignore-not-found", "-o", "name"]
    leftover = kubectl(cfg, get, capture=True).strip()
    if leftover:
        problems.append("ClusterRole seaweedfs-rw-cr exists: it grants pod writes in every namespace")
    problems += _undocumented_grants(cfg)
    if problems:
        raise E2EError("service-account RBAC exceeds the documented operations:\n" + "\n".join(problems))


def _undocumented_grants(cfg: E2EConfig) -> list[str]:
    """Roles bound to OLF ServiceAccounts that touch Secrets without resourceNames or create workloads."""
    namespaces = set(dict.fromkeys((cfg.platform_namespace, cfg.namespace)))
    bindings = [
        *json.loads(kubectl(cfg, ["get", "rolebinding", "-A", "-o", "json"], capture=True))["items"],
        *json.loads(kubectl(cfg, ["get", "clusterrolebinding", "-o", "json"], capture=True))["items"],
    ]
    problems = []
    for binding in bindings:
        subjects = binding.get("subjects") or []
        if not any(s.get("kind") == "ServiceAccount" and s.get("namespace") in namespaces for s in subjects):
            continue
        ref = binding["roleRef"]
        role_namespace = binding["metadata"].get("namespace")
        args = ["get", "role", ref["name"], "-n", role_namespace]
        if ref["kind"] != "Role":
            args = ["get", "clusterrole", ref["name"]]
        role = json.loads(kubectl(cfg, [*args, "-o", "json"], capture=True))
        if ref["name"].startswith(DOCUMENTED_ROLES):
            continue
        for rule in role.get("rules") or []:
            resources, verbs = set(rule.get("resources") or []), set(rule.get("verbs") or [])
            unpinned = not rule.get("resourceNames")
            reads_secrets = unpinned and "secrets" in resources and verbs & SECRET_VERBS
            runs_workloads = resources & WORKLOADS and verbs & {"create", "*"}
            if reads_secrets or runs_workloads:
                where = role_namespace or "cluster"
                problems.append(
                    f"{ref['kind']} {ref['name']} (bound in {where}) is not in ADR 0017: "
                    f"{sorted(resources)} {sorted(verbs)}"
                )
    return problems


def check_network_and_rbac(cfg: E2EConfig) -> None:
    document = load_flows(cfg)
    check_negative_flows(cfg, document)
    check_positive_flows(cfg, document)
    check_rbac(cfg)
