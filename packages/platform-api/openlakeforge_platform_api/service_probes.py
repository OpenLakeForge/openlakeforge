"""Service probes for platform health (ADR 0021, decision 7): one SERVICE `Signal` per enabled component.

The health routes call `ServiceProbes.probe`; nothing here is a route. Every probe is bounded by one timeout, and any
failure to get an answer (timeout, transport error, a refused permission) is `unknown`: a probe that could not look must
never read as healthy. A component the deployment leaves out is `disabled` without being probed.

What each probe proves, and what it does not (the table is ADR 0021, decision 7):

- `*.workloads`: pods ready, schedulable, not crash looping, and not restarting repeatedly in the last
  `restart_window`. Not that the service answers.
- `dagster.read-api`: the webserver answers GraphQL with a code location loaded. Not that a run would launch.
- `trino.readiness`: the coordinator finished starting. Not workers, catalogs or Polaris access.
- `object-store.list`: the endpoint is reachable and the key may list the bucket. Not that a write is durable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote

import requests
from botocore.exceptions import BotoCoreError, ClientError
from openlakeforge_core import HealthState, Scope, Signal, WorkloadObservation, evaluate_workload, service_signal
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient

log = logging.getLogger(__name__)

_SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_API_SERVER = "https://kubernetes.default.svc"
_DAGSTER_QUERY = """
{
  repositoriesOrError {
    __typename
    ... on RepositoryConnection { nodes { name } }
    ... on PythonError { message }
  }
}
"""

KubeList = Callable[[str], Mapping[str, Any]]
HttpGetJson = Callable[[str, float], Mapping[str, Any]]
DagsterQuery = Callable[[str, str], Mapping[str, Any]]


class ListableStore(Protocol):
    """The one S3 call the object-store probe makes; boto3's S3 client satisfies it."""

    def list_objects_v2(self, **kwargs: Any) -> Any: ...


class _Outcome(NamedTuple):
    state: HealthState
    reason: str
    evidence_uri: str | None = None


class ProbeError(Exception):
    """The probe could not get an answer, so the component's state is unknown."""


@dataclass(frozen=True)
class StageTargets:
    """What to probe in one stage. A URL left out means the component is not deployed there."""

    stage: str
    namespace: str
    dagster_graphql_url: str | None = None
    # The coordinator's base URL, e.g. http://trino.olf-dev:8080.
    trino_url: str | None = None
    # The bucket the object-store probe lists; None when no store is configured.
    bucket: str | None = None
    # Label selectors for the controllers of each component, from the chart's own labels.
    dagster_selector: Mapping[str, str] | None = None
    trino_selector: Mapping[str, str] | None = None


DEFAULT_DAGSTER_SELECTOR = {"app.kubernetes.io/name": "dagster"}
DEFAULT_TRINO_SELECTOR = {"app.kubernetes.io/name": "trino"}


class ServiceProbes:
    def __init__(
        self,
        *,
        kube_list: KubeList,
        http_get_json: HttpGetJson,
        dagster_query: DagsterQuery,
        store: ListableStore | None,
        timeout: timedelta = timedelta(seconds=5),
        ttl: timedelta = timedelta(seconds=60),
        restart_window: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._kube_list = kube_list
        self._http_get_json = http_get_json
        self._dagster_query = dagster_query
        self._store = store
        self._timeout = timeout
        self._ttl = ttl
        self._restart_window = restart_window
        self._clock = clock

    @classmethod
    def in_cluster(cls, store: ListableStore | None, **options: Any) -> ServiceProbes:
        return cls(
            kube_list=in_cluster_kube_list(),
            http_get_json=_http_get_json,
            dagster_query=lambda url, query: DagsterClient(url).graphql(query),
            store=store,
            **options,
        )

    def probe(self, targets: StageTargets) -> list[Signal]:
        """Signals for one stage; the probes run concurrently and share one timeout."""
        scope = Scope(stage=targets.stage)
        checks: list[tuple[str, bool, Callable[[], _Outcome]]] = [
            (
                "dagster.workloads",
                targets.dagster_graphql_url is not None,
                lambda: self._workloads(
                    targets, "dagster.workloads", targets.dagster_selector or DEFAULT_DAGSTER_SELECTOR
                ),
            ),
            ("dagster.read-api", targets.dagster_graphql_url is not None, lambda: self._dagster(targets)),
            (
                "trino.workloads",
                targets.trino_url is not None,
                lambda: self._workloads(targets, "trino.workloads", targets.trino_selector or DEFAULT_TRINO_SELECTOR),
            ),
            ("trino.readiness", targets.trino_url is not None, lambda: self._trino(targets)),
            (
                "object-store.list",
                targets.bucket is not None and self._store is not None,
                lambda: self._object_store(targets),
            ),
        ]
        now = self._clock()
        results = self._run([(name, check) for name, enabled, check in checks if enabled])
        signals = []
        for name, enabled, _ in checks:
            if not enabled:
                disabled = _Outcome(HealthState.DISABLED, "component is not enabled")
                signals.append(self._signal(scope, name, disabled, now))
            else:
                signals.append(self._signal(scope, name, results[name], now))
        return signals

    def _signal(self, scope: Scope, component: str, outcome: _Outcome, now: datetime) -> Signal:
        return service_signal(
            scope,
            component,
            outcome.state,
            outcome.reason,
            now=now,
            max_age=self._ttl,
            evidence_uri=outcome.evidence_uri,
        )

    def _run(
        self, checks: list[tuple[str, Callable[[], _Outcome]]]
    ) -> dict[str, _Outcome]:
        # A probe stuck in a socket cannot be cancelled, so it is abandoned (wait=False) rather than joined.
        pool = ThreadPoolExecutor(max_workers=max(1, len(checks)), thread_name_prefix="service-probe")
        deadline = monotonic() + self._timeout.total_seconds()
        futures = {name: pool.submit(check) for name, check in checks}
        results: dict[str, _Outcome] = {}
        try:
            for name, future in futures.items():
                try:
                    results[name] = future.result(timeout=max(0.0, deadline - monotonic()))
                except FutureTimeout:
                    results[name] = _Outcome(HealthState.UNKNOWN, "probe timed out")
                except ProbeError as exc:
                    results[name] = _Outcome(HealthState.UNKNOWN, str(exc))
                except Exception as exc:  # noqa: BLE001 - a probe bug must degrade one signal, not the whole answer
                    log.warning("service probe %s failed unexpectedly", name, exc_info=True)
                    results[name] = _Outcome(HealthState.UNKNOWN, f"probe failed: {type(exc).__name__}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    def _workloads(self, targets: StageTargets, component: str, selector: Mapping[str, str]) -> _Outcome:
        now = self._clock()
        observation = observe_workloads(
            self._kube_list, targets.namespace, selector, observed_at=now, restart_window=self._restart_window
        )
        # None, not a zero-replica observation: evaluate_workload reads desired == 0 as "scaled to zero" (WARNING),
        # and a selector that matches nothing is a wrong label or a missing release, which says nothing about health.
        signal = evaluate_workload(Scope(stage=targets.stage), component, observation, now=now, max_age=self._ttl)
        reason = signal.reason
        if observation is None:
            reason = f"no Deployment or StatefulSet matches {_selector_text(selector)} in {targets.namespace}"
        return _Outcome(signal.state, reason, signal.evidence_uri)

    def _dagster(self, targets: StageTargets) -> _Outcome:
        assert targets.dagster_graphql_url is not None
        try:
            data = self._dagster_query(targets.dagster_graphql_url, _DAGSTER_QUERY)
        except (ServiceClientError, requests.RequestException) as exc:
            raise ProbeError(f"Dagster did not answer: {exc}") from exc
        result = data.get("repositoriesOrError") or {}
        if result.get("__typename") == "PythonError":
            return _Outcome(HealthState.CRITICAL, "Dagster reports an error loading its repositories")
        if result.get("__typename") != "RepositoryConnection":
            raise ProbeError("Dagster's answer was not a repository list")
        if not result.get("nodes"):
            return _Outcome(HealthState.WARNING, "Dagster answers but has no code location loaded")
        return _Outcome(HealthState.HEALTHY, "Dagster answers and has a code location loaded")

    def _trino(self, targets: StageTargets) -> _Outcome:
        assert targets.trino_url is not None
        try:
            info = self._http_get_json(f"{targets.trino_url.rstrip('/')}/v1/info", self._timeout.total_seconds())
        except (requests.RequestException, ValueError) as exc:
            raise ProbeError(f"Trino did not answer: {exc}") from exc
        if info.get("starting") is True:
            return _Outcome(HealthState.WARNING, "Trino is still starting")
        if info.get("starting") is False:
            return _Outcome(HealthState.HEALTHY, "Trino coordinator has finished starting")
        raise ProbeError("Trino's answer did not say whether it is starting")

    def _object_store(self, targets: StageTargets) -> _Outcome:
        assert self._store is not None and targets.bucket is not None
        try:
            self._store.list_objects_v2(Bucket=targets.bucket, Prefix="", Delimiter="/", MaxKeys=1)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchBucket", "404"}:
                return _Outcome(HealthState.CRITICAL, "the bucket does not exist")
            raise ProbeError(f"the object store refused the listing ({code or 'unknown error'})") from exc
        except BotoCoreError as exc:
            raise ProbeError(f"the object store is unreachable: {type(exc).__name__}") from exc
        return _Outcome(HealthState.HEALTHY, "the bucket can be listed")


def _selector_text(selector: Mapping[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def observe_workloads(
    kube_list: KubeList,
    namespace: str,
    selector: Mapping[str, str],
    *,
    observed_at: datetime,
    restart_window: timedelta,
) -> WorkloadObservation | None:
    """Sum the matching Deployments and StatefulSets and look at their pods; None when nothing matches."""
    query = f"?labelSelector={quote(_selector_text(selector), safe='')}"
    controllers = [
        item
        for path in (
            f"/apis/apps/v1/namespaces/{namespace}/deployments",
            f"/apis/apps/v1/namespaces/{namespace}/statefulsets",
        )
        for item in kube_list(path + query).get("items", [])
    ]
    if not controllers:
        return None
    pods = kube_list(f"/api/v1/namespaces/{namespace}/pods").get("items", [])
    owned = [pod for pod in pods if any(_selects(c, pod) for c in controllers)]
    return WorkloadObservation(
        desired=sum(c.get("spec", {}).get("replicas", 1) for c in controllers),
        ready=sum(c.get("status", {}).get("readyReplicas", 0) for c in controllers),
        observed_at=observed_at,
        unschedulable=sum(_unschedulable(pod) for pod in owned),
        crash_looping=sum(_crash_looping(pod) for pod in owned),
        recent_restarts=max((_recent_restarts(pod, observed_at, restart_window) for pod in owned), default=0),
        evidence_uri=f"kubernetes://{namespace}",
    )


def _selects(controller: Mapping[str, Any], pod: Mapping[str, Any]) -> bool:
    wanted = controller.get("spec", {}).get("selector", {}).get("matchLabels", {})
    labels = pod.get("metadata", {}).get("labels", {})
    return bool(wanted) and all(labels.get(key) == value for key, value in wanted.items())


def _unschedulable(pod: Mapping[str, Any]) -> bool:
    return any(
        condition.get("type") == "PodScheduled"
        and condition.get("status") == "False"
        and condition.get("reason") == "Unschedulable"
        for condition in pod.get("status", {}).get("conditions", [])
    )


def _crash_looping(pod: Mapping[str, Any]) -> bool:
    return any(
        status.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
        for status in pod.get("status", {}).get("containerStatuses", [])
    )


def _recent_restarts(pod: Mapping[str, Any], now: datetime, window: timedelta) -> int:
    """The most restarts on one container whose latest termination finished inside the window.

    `restartCount` never resets and the API keeps only the latest termination, so a recent `finishedAt` is the
    only proof the count is current. The count of such a container is then an upper bound: some of its restarts
    may predate the window. A container that last died before the window counts as zero, however high its
    lifetime count, so a pod that settled does not stay on WARNING forever.
    """
    counts = [0]
    for status in pod.get("status", {}).get("containerStatuses", []):
        finished = _parse_time(status.get("lastState", {}).get("terminated", {}).get("finishedAt"))
        if finished is not None and finished >= now - window:
            counts.append(int(status.get("restartCount", 0)))
    return max(counts)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def in_cluster_kube_list() -> KubeList:
    """GET against the API server as the pod's ServiceAccount; a 401 or 403 is a ProbeError, never a guess."""

    def kube_list(path: str) -> Mapping[str, Any]:
        try:
            # Read per call: the kubelet rotates the projected token.
            token = (_SERVICE_ACCOUNT / "token").read_text().strip()
            response = requests.get(
                f"{_API_SERVER}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                verify=str(_SERVICE_ACCOUNT / "ca.crt"),
                timeout=5,
            )
        except (OSError, requests.RequestException) as exc:
            raise ProbeError(f"the Kubernetes API is unreachable: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise ProbeError(f"the Kubernetes API refused the read (HTTP {response.status_code})")
        if not response.ok:
            raise ProbeError(f"the Kubernetes API answered HTTP {response.status_code}")
        return dict(response.json())

    return kube_list


def _http_get_json(url: str, timeout: float) -> Mapping[str, Any]:
    response = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
    response.raise_for_status()
    return dict(response.json())
