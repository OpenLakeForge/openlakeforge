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
import fnmatch
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import yaml

from olf.common import log
from olf.common.deployment.context import stage_namespace
from olf.common.profile import StageName
from olf.infra import k8s
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl

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
SERVICE_ACCOUNT_GROUP = "system:serviceaccounts"
# Rules that touch Secrets or create workloads. `create` cannot be limited by resourceNames, so it counts.
SECRET_VERBS = {"get", "list", "watch", "create", "update", "patch", "delete", "*"}
WORKLOADS = {
    "pods", "jobs", "cronjobs", "deployments", "daemonsets", "statefulsets", "replicasets",
    "replicationcontrollers", "*",
}  # fmt: skip
# ADR 0017 E1: these ServiceAccounts may `create` Secrets namespace-wide and touch only the Secrets named here
# (fnmatch, first matching subject wins). The one place to change when the ADR's pins change.
E1_PINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("trino-credentials", ("trino-credential*", "trino-internal-secret", "trino-password-file")),
    ("postgresql-credentials", ("postgresql-*-creds",)),
    ("superset-credentials", ("superset-credentials", "superset-env")),
    (
        "seaweedfs-credentials",
        ("seaweedfs-s3-creds", "seaweedfs-*-s3-creds", "seaweedfs-s3-auth", "platform-api-log-reader-s3-creds"),
    ),
    ("*-credentials", ("*-client-*",)),  # dex-credentials
    ("*-cookie", ("*-cookie",)),
    ("*-oidc-secret", ("oidc-client-*", "*-client-*")),
    ("polaris-bootstrap", ("polaris-*",)),
    (
        "openmetadata-bootstrap",
        ("openmetadata-admin", "openmetadata-automation-bot", "openmetadata-ingestion-bot", "superset-credentials"),
    ),
    ("publish-local-ca", ("local-ca",)),
)
# ADR 0017 E2 to E5: (role kind, role glob, subject glob, the only resources a risky rule may name).
EXCEPTIONS: tuple[tuple[str, str, str, frozenset[str]], ...] = (
    ("Role", "openmetadata*", "openmetadata*", frozenset({"pods", "configmaps", "secrets", "jobs", "cronjobs"})),  # E2
    ("Role", "dagster*", "dagster*", frozenset({"pods", "jobs"})),  # E3
    ("ClusterRole", "traefik*", "traefik*", frozenset({"secrets"})),  # E4
    ("Role", "cert-manager*", "cert-manager*", frozenset({"secrets"})),  # E5: the webhook's dynamic-serving Role
    ("ClusterRole", "cert-manager*", "cert-manager*", frozenset({"secrets", "pods", "services"})),  # E5 + solvers
)


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


def stage_wide(document: dict[str, Any]) -> set[str]:
    """Protected workloads that every stage pod may reach: the rows whose source is the whole stage namespace."""
    return {
        workload
        for workload in PROTECTED
        for row in document["flows"]
        if row["from"] == {"scope": "stage"} and row["to"].get("pods") == document["workloads"][workload]
    }


def workload_ports(document: dict[str, Any], workload: str) -> list[int]:
    """The ports a flow row opens to the workload; the served one varies (Trino: 8080, or 8443 with TLS)."""
    labels = document["workloads"][workload]
    for row in document["flows"]:
        if row["to"].get("pods") == labels:
            return [int(port) for port in row["ports"]]
    raise E2EError(f"no flow row reaches workload {workload}")


def _pod_ip(cfg: E2EConfig, namespace: str, labels: dict[str, str]) -> str | None:
    out = kubectl(
        cfg,
        ["get", "pod", "-n", namespace, "-l", _selector(labels), "--field-selector=status.phase=Running",
         "-o", "jsonpath={.items[*].status.podIP}"],
        capture=True,
    ).split()
    # `items[0]` on an empty list is a kubectl error; a workload that is not deployed is not one.
    return out[0] if out else None


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
        targets[workload] = (ip, workload_ports(document, workload))
    if not targets:
        raise E2EError("no protected backend is running, so the negative flows prove nothing")
    # An interrupted run leaves the namespace behind, possibly terminating; wait for it to go.
    kubectl(cfg, ["delete", "namespace", UNTRUSTED_NAMESPACE, "--ignore-not-found", "--timeout=180s"])
    kubectl(cfg, ["create", "namespace", UNTRUSTED_NAMESPACE])
    problems: list[str] = []
    try:
        # (namespace, name, allowed): the probes are unlabelled, so only the stage-wide rows (F6, F9, F10, F12) apply.
        cases = [
            (UNTRUSTED_NAMESPACE, "outside", set()),
            (cfg.namespace, "stage", stage_wide(document)),
            (cfg.platform_namespace, "platform", set()),
        ]
        for namespace, name, allowed in cases:
            with _probe_pod(cfg, namespace, {}, f"olf-e2e-deny-{name}") as pod:
                for workload, (ip, ports) in targets.items():
                    rcs = {port: _connect(cfg, namespace, pod, ip, port) for port in ports}
                    if workload in allowed:
                        # a control: a broken probe cannot pass vacuously. One served port is enough.
                        if all(rc in NOT_REACHED for rc in rcs.values()):
                            problems.append(f"control {namespace} -> {workload}:{ports} did not connect (rc={rcs})")
                    else:
                        problems += [
                            f"{namespace} -> {workload} {ip}:{port} was not dropped (rc={rc})"
                            for port, rc in rcs.items()
                            if rc != 28
                        ]
    finally:
        kubectl(cfg, ["delete", "namespace", UNTRUSTED_NAMESPACE, "--ignore-not-found", "--wait=false"])
    if problems:
        raise E2EError("network isolation breach:\n" + "\n".join(problems))


def _can_i(cfg: E2EConfig, subject: str, *args: str) -> bool:
    # `can-i` exits 1 on "no"; the answer, not the exit code, is the result.
    argv = ["auth", "can-i", f"--as={subject}", *args]
    out = k8s._kubectl(argv, capture=True, check=False, kube_context=cfg.kube_context).strip()
    answer = out.split(maxsplit=1)[0] if out else ""  # some versions print "no - <reason>"
    if answer not in ("yes", "no"):
        # An API, auth or impersonation error prints nothing on stdout; reading that as "no" would pass the check.
        raise E2EError(f"kubectl auth can-i gave no answer for {subject} {' '.join(args)}: {out!r}")
    return answer == "yes"


def _olf_namespaces(cfg: E2EConfig) -> list[str]:
    """The platform namespace, the selected stage, and every other deployed stage."""
    found = list(dict.fromkeys((cfg.platform_namespace, cfg.namespace)))
    for stage in StageName:
        namespace = stage_namespace(stage)
        get = ["get", "namespace", namespace, "--ignore-not-found", "-o", "name"]
        if namespace not in found and kubectl(cfg, get, capture=True).strip():
            found.append(namespace)
    return found


def _service_accounts(cfg: E2EConfig) -> list[tuple[str, str]]:
    found = []
    for namespace in _olf_namespaces(cfg):
        get = ["get", "serviceaccount", "-n", namespace, "-o", "json"]
        items = json.loads(kubectl(cfg, get, capture=True))["items"]
        found += [(namespace, item["metadata"]["name"]) for item in items]
    return found


def rbac_denials(namespace: str, name: str) -> list[tuple[str, ...]]:
    """can-i arguments that must answer no for this ServiceAccount (ADR 0017, "Denied")."""
    denied: list[tuple[str, ...]] = [
        *[(verb, "roles", "-n", namespace) for verb in ("escalate", "bind")],
        *[(verb, "clusterroles") for verb in ("escalate", "bind")],
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
    problems += _undocumented_grants(cfg, accounts)
    problems += _label_drift(cfg)
    if problems:
        raise E2EError("service-account RBAC exceeds the documented operations:\n" + "\n".join(problems))


def _subjects(binding: dict[str, Any], accounts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The OLF ServiceAccounts a binding reaches, expanding the `system:serviceaccounts[:<ns>]` groups."""
    found: set[tuple[str, str]] = set()
    for subject in binding.get("subjects") or []:
        name = subject.get("name", "")
        if subject.get("kind") == "ServiceAccount":
            if subject.get("namespace") in {a[0] for a in accounts}:
                found.add((subject["namespace"], name))
        elif subject.get("kind") == "Group" and name == SERVICE_ACCOUNT_GROUP:
            found |= set(accounts)
        elif subject.get("kind") == "Group" and name.startswith(SERVICE_ACCOUNT_GROUP + ":"):
            found |= {a for a in accounts if a[0] == name.removeprefix(SERVICE_ACCOUNT_GROUP + ":")}
    return sorted(found)


def _rule_problem(kind: str, role: str, subject: str, rule: dict[str, Any]) -> bool:
    """True when this rule is not covered by ADR 0017 for the subject (E1 pins, E2 to E5 tuples)."""
    resources, verbs = set(rule.get("resources") or []), set(rule.get("verbs") or [])
    names = rule.get("resourceNames") or []
    pins = next((patterns for glob, patterns in E1_PINS if fnmatch.fnmatch(subject, glob)), None)
    if pins is not None:
        if resources & WORKLOADS:
            return bool(verbs & {"create", "*"})
        if not resources & {"secrets", "*"}:
            return False
        if not names:  # namespace-wide: only the E1 `create`
            return not (verbs == {"create"} and resources <= {"secrets", "configmaps"})
        return not all(any(fnmatch.fnmatch(n, p) for p in pins) for n in names)
    reads_secrets = resources & {"secrets", "*"} and verbs & SECRET_VERBS
    runs_workloads = resources & WORKLOADS and verbs & {"create", "*"}
    if not (reads_secrets or runs_workloads):
        return False
    return not any(
        kind == k and fnmatch.fnmatch(role, r) and fnmatch.fnmatch(subject, s) and resources <= allowed
        for k, r, s, allowed in EXCEPTIONS
    )


def grant_problems(
    bindings: list[dict[str, Any]],
    get_role: Callable[[dict[str, Any], str | None], dict[str, Any]],
    accounts: list[tuple[str, str]],
) -> list[str]:
    """Each (role, subject, rule) tuple reaching an OLF ServiceAccount that ADR 0017 does not name."""
    problems = []
    for binding in bindings:
        subjects = _subjects(binding, accounts)
        if not subjects:
            continue
        ref, role_namespace = binding["roleRef"], binding["metadata"].get("namespace")
        role = get_role(ref, role_namespace)
        for namespace, name in subjects:
            for rule in role.get("rules") or []:
                if _rule_problem(ref["kind"], ref["name"], name, rule):
                    problems.append(
                        f"{ref['kind']} {ref['name']} (bound in {role_namespace or 'cluster'}) gives "
                        f"{namespace}/{name} what ADR 0017 does not name: {rule.get('resources')} "
                        f"{rule.get('verbs')} {rule.get('resourceNames') or ''}".rstrip()
                    )
    return problems


def _undocumented_grants(cfg: E2EConfig, accounts: list[tuple[str, str]]) -> list[str]:
    bindings = [
        *json.loads(kubectl(cfg, ["get", "rolebinding", "-A", "-o", "json"], capture=True))["items"],
        *json.loads(kubectl(cfg, ["get", "clusterrolebinding", "-o", "json"], capture=True))["items"],
    ]

    def get_role(ref: dict[str, Any], namespace: str | None) -> dict[str, Any]:
        args = ["get", "role", ref["name"], "-n", namespace or ""]
        if ref["kind"] != "Role":
            args = ["get", "clusterrole", ref["name"]]
        return json.loads(kubectl(cfg, [*args, "-o", "json"], capture=True))

    return grant_problems(bindings, get_role, accounts)


def label_drift(workloads: dict[str, dict[str, str]], pods: list[dict[str, Any]]) -> list[str]:
    """Workloads with a deployed pod under their first label but none carrying all the flow file's labels.

    A policy selecting labels no pod carries matches nothing, so the probes would skip it silently.
    """
    problems = []
    for workload, labels in workloads.items():
        key, value = next(iter(labels.items()))
        deployed = [p for p in pods if p["metadata"].get("labels", {}).get(key) == value]
        if deployed and not any(labels.items() <= p["metadata"].get("labels", {}).items() for p in deployed):
            problems.append(f"workload {workload}: deployed pods do not carry the flow file's labels {labels}")
    return problems


def _label_drift(cfg: E2EConfig) -> list[str]:
    pods = json.loads(kubectl(cfg, ["get", "pods", "-n", cfg.platform_namespace, "-o", "json"], capture=True))
    return label_drift(load_flows(cfg)["workloads"], pods["items"])


def check_network_and_rbac(cfg: E2EConfig) -> None:
    document = load_flows(cfg)
    check_negative_flows(cfg, document)
    check_positive_flows(cfg, document)
    check_rbac(cfg)
