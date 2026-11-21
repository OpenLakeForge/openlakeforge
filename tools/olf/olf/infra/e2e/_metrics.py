"""The metrics read path's acceptance (ADR 0024, slice M-f, #21): series through the API, labels, budget, metrics off.

On a deployment with metrics on, every source of decision 2 answers at least one non-empty series through the
secured platform API, for a role at the routes' floor, and a role below it gets the same 403 as the stage health
route. Prometheus is then asked directly, from a platform API pod (F45's own path), for its head series and for
every `olf:` series, so decision 3's budget and label allowlist are measured on the running stack rather than read
from the rule file. With metrics off, nothing of the backend exists, and the stage's status, logs and quality still
answer.

Role tokens come from the e2e GitHub stub through Dex, as the perimeter checks get theirs; a profile without that
stub skips the API reads, and says so.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
from openlakeforge_core.metrics import MetricScope, MetricSource, catalog_for

from olf.common import config, log
from olf.infra import access
from olf.infra.e2e import _log_delivery as delivery
from olf.infra.e2e._identity_github import REQUEST_TIMEOUT, dex_points_at_stub, github_stub, log_skip
from olf.infra.e2e._perimeter_bearer import CLI_CLIENT, PLATFORM_API_HOST_REF, issue_id_token
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl, load_provider_contracts_or_raise

# ADR 0024, decision 3: the labels a served series may carry, and the head-series operating target.
ALLOWED_LABELS = frozenset({"stage", "component", "service", "domain"})
SERIES_BUDGET = 10_000
# The metrics rows' floor in permissions.toml, and the stage route whose refusal theirs must match.
METRICS_FLOOR = "data-engineer"
REFERENCE_OPERATION = "get_stage_health"
METRIC_WORKLOADS = ("prometheus", "kube-state-metrics")
# Long enough to cover the whole suite, so a series sampled early in the run (a bucket's size gauge, which SeaweedFS
# drops after ten idle minutes) still counts.
READ_RANGE = "6h"
# ID tokens live five minutes; one issued this long ago is replaced before it is sent.
TOKEN_REUSE_SECONDS = 240
PLATFORM_API_SERVICE = "platform-api"
PLATFORM_API_PORT = 8080

# Each source of decision 2, and the series any one of which proves it answers. Dagster's two are each required.
STAGE_SOURCES: dict[str, tuple[str, ...]] = {
    "kubelet": ("cpu_usage_cores", "memory_working_set_bytes"),
    "kube-state-metrics": ("container_restarts",),
    "trino": ("trino_heap_used_bytes", "trino_running_queries", "trino_queued_queries", "trino_failed_queries"),
    "seaweedfs": ("object_store_bucket_bytes",),
    "dagster runs": ("pipeline_runs",),
    "dagster durations": ("pipeline_run_duration_seconds",),
}
PLATFORM_SOURCES: dict[str, tuple[str, ...]] = {
    "kubelet nodes": ("node_cpu_usage_cores", "node_memory_working_set_bytes"),
    "seaweedfs volumes": ("object_store_disk_used_bytes", "object_store_disk_free_bytes"),
}


def has_points(envelope: Mapping[str, Any]) -> bool:
    """Whether a series envelope is `available` with at least one point that is not null."""
    return envelope.get("state") == "available" and any(
        point[1] is not None for line in envelope.get("lines", []) for point in line.get("points", [])
    )


def missing_sources(
    envelopes: Mapping[str, Mapping[str, Any]], sources: Mapping[str, Sequence[str]]
) -> dict[str, list[str]]:
    """Each source none of whose series has a point, with the state and reason each of them was read with."""
    missing: dict[str, list[str]] = {}
    for source, names in sources.items():
        if not any(has_points(envelopes.get(name, {})) for name in names):
            missing[source] = [
                f"{name}: {envelopes.get(name, {}).get('state', 'not read')} ({envelopes.get(name, {}).get('reason')})"
                for name in names
            ]
    return missing


def same_refusal(
    reference: Mapping[str, Any], reference_operation: str, got: Mapping[str, Any], operation: str
) -> bool:
    """Whether a 403 body is the reference route's, naming its own operation where the reference names its."""
    return got.get("detail") == str(reference.get("detail", "")).replace(f"'{reference_operation}'", f"'{operation}'")


def parse_query(body: str) -> list[tuple[dict[str, str], float]]:
    """The labels and value of each sample of a Prometheus `/api/v1/query` vector answer."""
    payload = json.loads(body)
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "vector":
        raise E2EError(f"Prometheus did not answer the query with a vector: {body[:300]}")
    return [(dict(item["metric"]), float(item["value"][1])) for item in payload["data"]["result"]]


def parse_series(body: str) -> list[dict[str, str]]:
    """The label sets of a Prometheus `/api/v1/series` answer."""
    payload = json.loads(body)
    if payload.get("status") != "success":
        raise E2EError(f"Prometheus did not answer the series request: {body[:300]}")
    return [dict(labels) for labels in payload["data"]]


def label_violations(series: Iterable[Mapping[str, str]], allowed: frozenset[str] = ALLOWED_LABELS) -> list[str]:
    """`name{label}` for each label of a series outside `allowed`; `__name__` is the series' name, not a label."""
    return sorted(
        {
            f"{labels.get('__name__', '?')}{{{name}}}"
            for labels in series
            for name in labels
            if name != "__name__" and name not in allowed
        }
    )


def budget_problem(head_series: float, budget: int = SERIES_BUDGET) -> str | None:
    if head_series >= budget:
        return f"Prometheus holds {head_series:.0f} head series, at or over the {budget} series budget"
    return None


def metric_objects(items: Iterable[Mapping[str, Any]]) -> list[str]:
    """`Kind/name` of each object that belongs to the metrics backend, by name or by its pods' name label."""
    found = []
    for item in items:
        metadata = item.get("metadata", {})
        label = metadata.get("labels", {}).get("app.kubernetes.io/name")
        if metadata.get("name") in METRIC_WORKLOADS or label in METRIC_WORKLOADS:
            found.append(f"{item.get('kind')}/{metadata.get('name')}")
    return found


def _selects_metrics(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("app.kubernetes.io/name") in METRIC_WORKLOADS:
            return True
        return any(_selects_metrics(child) for child in value.values())
    if isinstance(value, list):
        return any(_selects_metrics(child) for child in value)
    return False


def metric_policies(policies: Iterable[Mapping[str, Any]]) -> list[str]:
    """`namespace/name` of each NetworkPolicy that selects or admits a metrics pod: a flow row left behind."""
    return [
        f"{p['metadata'].get('namespace')}/{p['metadata']['name']}"
        for p in policies
        if p["metadata"]["name"] in {f"olf-flows-{name}" for name in METRIC_WORKLOADS} or _selects_metrics(p["spec"])
    ]


def _stage(cfg: E2EConfig) -> str:
    return cfg.namespace.removeprefix("olf-")


def metrics_contract(cfg: E2EConfig) -> Mapping[str, Any]:
    return load_provider_contracts_or_raise(cfg)["shared"]["observability"]["metrics"]


def metrics_on(contract: Mapping[str, Any]) -> bool:
    return bool(contract.get("enabled") and contract.get("supported") and contract.get("query_url"))


# Longer than the platform API's slowest read (the run routes' 20 s, `run_failure.READ_DEADLINE_SECONDS`, and a log
# read's 5 s wait for a worker before its own 20 s, `log_reads`), so the client never gives up on an answer the API is
# still allowed to give, and a 502 past the deadline or a 503 for a busy pool is seen as one.
API_READ_TIMEOUT = 30


@dataclass
class ApiSession:
    """The platform API on the portal's host, and fresh role tokens for it."""

    route: access.Route
    base_url: str
    token: Callable[[str], str]
    reader: str
    refused: str | None

    def get(self, path: str, role: str) -> requests.Response:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token(role)}"}
        return self.route.session().get(
            f"{self.base_url}{path}", headers=headers, allow_redirects=False, timeout=API_READ_TIMEOUT
        )

    def read(self, path: str, role: str | None = None) -> dict[str, Any]:
        reply = self.get(path, role or self.reader)
        if reply.status_code != 200:
            raise E2EError(f"{path} for {role or self.reader}: HTTP {reply.status_code} {reply.text[:300]}")
        return dict(reply.json())


def session_roles(
    precedence: Sequence[str],
    mapping: Mapping[str, Any],
    floor: str,
    admits: Callable[[str], bool] | None = None,
) -> tuple[str | None, str | None]:
    """The mapped role nearest above `floor` that `admits` lets in, and the first mapped role below `floor`.

    `admits` narrows the reader further than the floor, for a row whose `role_stages` refuses the floor's own role
    on some stages; without it every role at or above the floor reads.
    """
    index = precedence.index(floor)
    reader = next(
        (r for r in reversed(precedence[: index + 1]) if r in mapping and (admits is None or admits(r))), None
    )
    refused = next((r for r in precedence[index + 1 :] if r in mapping), None)
    return reader, refused


@contextlib.contextmanager
def api_session(
    cfg: E2EConfig,
    *,
    floor: str = METRICS_FLOOR,
    admits: Callable[[Sequence[str]], Callable[[str], bool]] | None = None,
    what: str = "the metrics API reads",
) -> Iterator[ApiSession | None]:
    """Sign in through Dex against the GitHub stub as a role at `floor` and one below it.

    `admits`, given the role precedence, says which roles may read the routes on this stage (`session_roles`).
    Yields None, having said why, when the deployment has no such path: no perimeter, no Dex, no `olf-cli` client,
    no platform API route, or a port-forward run that bypasses the ingress.
    """
    contracts = load_provider_contracts_or_raise(cfg)
    access_contract, identity = contracts["shared"]["access"], contracts["shared"]["identity"]
    routes = {ref: route["url"] for ref, route in access_contract.get("routes", {}).items()}
    reasons = {
        "the contract declares no enabled perimeter": not (access_contract.get("perimeter") or {}).get("enabled"),
        "they need the Dex adapter": identity.get("adapter") != "dex",
        f"the contract has no {CLI_CLIENT} client": CLI_CLIENT not in identity.get("clients", {}),
        "the contract has no platform API route": PLATFORM_API_HOST_REF not in routes,
        "OLF_PORT_FORWARD bypasses the ingress": config.truthy(cfg_environ(cfg).get(access.PORT_FORWARD_ENV, "")),
    }
    if reason := next((text for text, applies in reasons.items() if applies), None):
        log.info(f"Skipping {what}: {reason}.")
        yield None
        return
    with access.service_route(
        "shared/identity",
        service="dex",
        remote_port=5556,
        namespace=cfg.platform_namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-dex-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg_environ(cfg),
    ) as route:
        if not dex_points_at_stub(cfg):
            log_skip()
            yield None
            return
        precedence, mapping = identity["roles"]["precedence"], identity["role_mapping"]
        reader, refused = session_roles(precedence, mapping, floor, admits(precedence) if admits else None)
        if reader is None:
            raise E2EError(f"the profile maps no team to a role at or above {floor} that may read {what}")
        suffix = secrets.token_hex(3)
        logins = {role: f"olf-e2e-api-{role}-{suffix}" for role in (reader, refused) if role}
        client_id = identity["clients"][CLI_CLIENT]["client_id"]
        loopback = f"http://127.0.0.1:{20000 + secrets.randbelow(40000)}/callback"
        issued: dict[str, tuple[float, str]] = {}

        def token(role: str) -> str:
            if role not in issued or time.monotonic() - issued[role][0] > TOKEN_REUSE_SECONDS:
                issued[role] = (
                    time.monotonic(),
                    issue_id_token(route, cfg, identity, logins[role], client_id, loopback),
                )
            return issued[role][1]

        with github_stub(cfg, {logins[role]: mapping[role] for role in logins}):
            yield ApiSession(route, routes[PLATFORM_API_HOST_REF].rstrip("/"), token, reader, refused)


def stage_series_paths(stage: str, metric_range: str = READ_RANGE) -> dict[str, str]:
    return {
        s.name: f"/api/v1/stages/{stage}/metrics/{s.name}?range={metric_range}" for s in catalog_for(MetricScope.STAGE)
    }


def platform_series_paths(metric_range: str = READ_RANGE) -> dict[str, str]:
    return {
        s.name: f"/api/v1/platform/metrics/{s.name}?range={metric_range}" for s in catalog_for(MetricScope.PLATFORM)
    }


def _check_refusals(api: ApiSession, stage: str) -> None:
    if api.refused is None:
        log.info(f"Not checking a refusal: the profile maps no team to a role below {METRICS_FLOOR}.")
        return
    reference = api.get(f"/api/v1/stages/{stage}/health", api.refused)
    if reference.status_code != 403:
        raise E2EError(f"{api.refused} reading the stage health route: expected HTTP 403, got {reference.status_code}")
    any_stage, any_platform = (
        next(iter(stage_series_paths(stage).values())),
        next(iter(platform_series_paths().values())),
    )
    for operation, path in (
        ("list_stage_metrics", f"/api/v1/stages/{stage}/metrics"),
        ("get_stage_metric", any_stage),
        ("get_platform_metric", any_platform),
    ):
        reply = api.get(path, api.refused)
        if reply.status_code != 403 or not same_refusal(reference.json(), REFERENCE_OPERATION, reply.json(), operation):
            raise E2EError(
                f"{api.refused} at {path}: expected the stage health route's 403 ({reference.json()}), "
                f"got HTTP {reply.status_code} {reply.text[:300]}"
            )


def check_metrics_reads(cfg: E2EConfig) -> None:
    """Every source answers a non-empty series through the API; a role below the floor is refused as elsewhere."""
    if not metrics_on(contract := metrics_contract(cfg)):
        log.info(f"Skipping the metrics API reads: metrics are not served on this deployment ({dict(contract)}).")
        return
    stage = _stage(cfg)
    with api_session(cfg) as api:
        if api is None:
            return
        log.step(f"Reading one series of each metrics source through the platform API as {api.reader}...")
        catalog = api.read(f"/api/v1/stages/{stage}/metrics")
        listed = {entry["name"] for entry in catalog["series"]}
        expected = {s.name for s in catalog_for(MetricScope.STAGE)}
        if listed != expected:
            raise E2EError(f"the stage catalog lists {sorted(listed)}, expected {sorted(expected)}")
        stage_envelopes = {name: api.read(path) for name, path in stage_series_paths(stage).items()}
        platform_envelopes = {name: api.read(path) for name, path in platform_series_paths().items()}
        missing = {
            **missing_sources(stage_envelopes, STAGE_SOURCES),
            **missing_sources(platform_envelopes, PLATFORM_SOURCES),
        }
        for name, envelope in {**stage_envelopes, **platform_envelopes}.items():
            log.info(f"  {name}: {envelope['state']}, {len(envelope['lines'])} lines ({envelope['reason']})")
        if missing:
            raise E2EError(f"metrics sources with no series through the API: {missing}")
        _check_refusals(api, stage)


def _prometheus_get(cfg: E2EConfig, query_url: str, path: str, params: Mapping[str, str]) -> str:
    """GET Prometheus's HTTP API from a platform API pod: F45 admits that pod and nothing else."""
    url = f"{query_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    api_pod = delivery._api_pod(cfg)  # noqa: SLF001 - one helper, one home
    return kubectl(cfg, delivery.exec_get_args(cfg.platform_namespace, api_pod, url), capture=True)


def head_series(cfg: E2EConfig, query_url: str) -> float:
    samples = parse_query(_prometheus_get(cfg, query_url, "/api/v1/query", {"query": "prometheus_tsdb_head_series"}))
    if len(samples) != 1:
        raise E2EError(f"expected one prometheus_tsdb_head_series sample, got {samples}")
    return samples[0][1]


def check_metrics_cardinality(cfg: E2EConfig) -> None:
    """Head series under decision 3's budget, and no `olf:` series with a label outside its allowlist."""
    if not metrics_on(contract := metrics_contract(cfg)):
        log.info("Skipping the metrics cardinality check: metrics are not served on this deployment.")
        return
    log.step("Checking the metrics backend's head series and the labels of every olf: series...")
    query_url = str(contract["query_url"])
    heads = head_series(cfg, query_url)
    start = str(int(datetime.now(UTC).timestamp()) - 24 * 3600)
    rules = parse_series(
        _prometheus_get(cfg, query_url, "/api/v1/series", {"match[]": '{__name__=~"olf:.+"}', "start": start})
    )
    log.info(f"Prometheus holds {heads:.0f} head series; {len(rules)} olf: series in the last day.")
    served = {s.rule for s in catalog_for(MetricScope.STAGE) + catalog_for(MetricScope.PLATFORM) if s.rule}
    if absent := served - {labels.get("__name__") for labels in rules}:
        raise E2EError(f"recording rules with no series in the last day: {sorted(absent)}")
    if violations := label_violations(rules):
        raise E2EError(f"olf: series carry labels outside {sorted(ALLOWED_LABELS)}: {violations}")
    if problem := budget_problem(heads):
        raise E2EError(problem)


def check_metrics_off(cfg: E2EConfig) -> None:
    """With metrics off: no backend object or flow, and the stage's status, logs and quality still answer."""
    if metrics_contract(cfg).get("enabled"):
        log.info("Skipping the metrics-off check: this deployment turns metrics on.")
        return
    log.step("Checking a deployment with metrics off has no metrics backend...")
    kinds = "deployments,pods,persistentvolumeclaims,services,configmaps,serviceaccounts"
    items = json.loads(kubectl(cfg, ["get", kinds, "-n", cfg.platform_namespace, "-o", "json"], capture=True))["items"]
    roles = kubectl(
        cfg,
        ["get", "clusterrole", f"{cfg.platform_namespace}-prometheus", "--ignore-not-found", "-o", "name"],
        capture=True,
    ).split()
    policies = json.loads(kubectl(cfg, ["get", "networkpolicy", "-A", "-o", "json"], capture=True))["items"]
    if left := metric_objects(items) + roles + metric_policies(policies):
        raise E2EError(f"metrics are off, but the metrics backend left: {left}")
    _check_api_without_metrics(cfg)


def _check_api_without_metrics(cfg: E2EConfig) -> None:
    """The stage's routes as an anonymous caller: a deployment with no issuer answers every caller as one role."""
    if not kubectl(
        cfg,
        ["get", "service", PLATFORM_API_SERVICE, "-n", cfg.platform_namespace, "--ignore-not-found", "-o", "name"],
        capture=True,
    ).strip():
        log.info("Not reading the platform API with metrics off: this deployment has none.")
        return
    stage, day = _stage(cfg), datetime.now(UTC).date().isoformat()
    with access.service_route(
        PLATFORM_API_HOST_REF,
        service=PLATFORM_API_SERVICE,
        remote_port=PLATFORM_API_PORT,
        namespace=cfg.platform_namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-platform-api-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
        environ=cfg_environ(cfg),
    ) as route:

        def read(path: str) -> dict[str, Any]:
            reply = route.session().get(
                f"{route.url}{path}", headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT
            )
            if reply.status_code == 401:
                raise _NeedsToken
            if reply.status_code != 200:
                raise E2EError(f"{path} with metrics off: HTTP {reply.status_code} {reply.text[:300]}")
            return dict(reply.json())

        try:
            health = read(f"/api/v1/stages/{stage}/health")
        except _NeedsToken:
            log.info("Not reading the platform API with metrics off: it needs a token, and only Full has the stub.")
            return
        integration = {i["name"]: i["status"] for i in health["integrations"]}.get("metrics")
        if integration != "disabled":
            raise E2EError(f"the stage health route's metrics integration reads {integration!r}, expected 'disabled'")
        read(f"/api/v1/stages/{stage}/health/quality")
        read(f"/api/v1/stages/{stage}/logs?date={day}&limit=1")
        prometheus = next(s.name for s in catalog_for(MetricScope.STAGE) if s.source is MetricSource.PROMETHEUS)
        if (state := read(f"/api/v1/stages/{stage}/metrics/{prometheus}?range=1h")["state"]) != "disabled":
            raise E2EError(f"{prometheus} reads {state!r} with metrics off, expected 'disabled'")
        log.info("With metrics off, stage health, quality and logs answer and the metrics series read 'disabled'.")


class _NeedsToken(Exception):
    pass
