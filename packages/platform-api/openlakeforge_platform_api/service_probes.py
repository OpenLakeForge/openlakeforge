"""Service probes for platform health (ADR 0021, decision 7): one SERVICE `Signal` per enabled component.

The health routes call `ServiceProbes.probe`; nothing here is a route. Every probe is bounded by one timeout, and any
failure to get an answer (timeout, transport error, a refused permission) is `unknown`: a probe that could not look must
never read as healthy. A component the deployment leaves out is `disabled` without being probed.

What each probe proves, and what it does not (the table is ADR 0021, decision 7):

- `*.workloads`: pods ready, schedulable, not crash looping, and not restarting repeatedly in the last
  `restart_window`. Not that the service answers.
- `dagster.read-api`: the webserver answers GraphQL with a code location loaded. Not that a run would launch.
- `trino.readiness`: the coordinator finished starting. Not workers, catalogs or Polaris access.
- `polaris.readiness`: the catalog's management port answers its readiness check without a credential. Not that a
  catalog read works, that a grant holds or that the metastore holds the stage's catalog.
- `object-store.list`: the endpoint is reachable and the key may list the bucket. Not that a write is durable.
- `log-archive.spool-age`, `log-archive.spool-size`: how old the collector's oldest unsent file is and how full its
  spool is, over every collector pod (ADR 0022, decision 2). Not that an object reached the bucket.
- `log-archive.newest-object`: the stage's newest archive object, listed with the log reader's key. Covers a
  collector that is down, which the spool probe cannot see; an idle stage reads the same as a stalled one.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote, urlsplit

import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from openlakeforge_core import (
    ArchiveThresholds,
    HealthState,
    Scope,
    Signal,
    SpoolObservation,
    WorkloadObservation,
    evaluate_newest_object,
    evaluate_spool_age,
    evaluate_spool_size,
    evaluate_workload,
    parse_spool_report,
    service_signal,
    worst_state,
)
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.dagster import DagsterClient

from openlakeforge_platform_api.spool_probe import PATH as SPOOL_PATH

log = logging.getLogger(__name__)

_SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_API_SERVER = "https://kubernetes.default.svc"
# observe_workloads lists deployments, statefulsets, then pods.
_CALLS_PER_WORKLOAD_PROBE = 3
_DAGSTER_QUERY = """
{
  repositoriesOrError {
    __typename
    ... on RepositoryConnection { nodes { name } }
    ... on PythonError { message }
  }
}
"""

# SmallRye Health, under Polaris's Quarkus management interface, answers a failed readiness check with this status.
_NOT_READY = 503

KubeList = Callable[[str], Mapping[str, Any]]
HttpGetJson = Callable[[str, float], Mapping[str, Any]]
DagsterQuery = Callable[[str, str], Mapping[str, Any]]
# What the collector pods' probes answered: the observations, and how many pods did not answer.
SpoolRead = Callable[[str], tuple[list[SpoolObservation], int]]
# The newest archive objects listed under a stage's prefix are looked for over this many days: the UTC day of an
# object is its records' day, so an outage across midnight still shows yesterday's objects.
ARCHIVE_DAYS = 2
# A day's prefix holds one object per flush per node, far below this; the cap bounds a probe on a runaway prefix.
_ARCHIVE_PAGES = 5


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
    # The catalog's management base URL, e.g. http://polaris-mgmt.olf-system.svc:8182. Polaris is one service for every
    # stage, so each stage reads the same answer; None when the profile deploys no catalog.
    polaris_health_url: str | None = None
    # The bucket the object-store probe lists; None when no store is configured.
    bucket: str | None = None
    # Label selectors for the controllers of each component, from the chart's own labels.
    dagster_selector: Mapping[str, str] | None = None
    trino_selector: Mapping[str, str] | None = None
    # The stage's archive: s3://<ops>/activations/<stage>/logs and the namespace it collects. The newest-object probe
    # lists `<base>/v2/namespace=<namespace>/date=<day>/`.
    log_archive_base_uri: str | None = None
    log_archive_namespace: str | None = None
    # The collector probes' base URL (the headless Service); the spool of this stage is read from every pod.
    spool_probe_url: str | None = None


class StageProber(Protocol):
    """What the health routes need of `ServiceProbes`: a blocking call that returns one signal per component."""

    def probe(self, targets: StageTargets) -> list[Signal]: ...


# Each stage read would otherwise fan out to up to nine backend calls; dashboards poll, so reuse a recent answer.
DEFAULT_PROBE_CACHE_TTL = timedelta(seconds=10)
# Every probe of one stage shares this budget, so a stage's service answer takes about this long at most.
PROBE_TIMEOUT = timedelta(seconds=5)
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
        archive_store: ListableStore | None = None,
        spool_read: SpoolRead | None = None,
        archive_thresholds: ArchiveThresholds = ArchiveThresholds(),  # noqa: B008 - frozen
        timeout: timedelta = PROBE_TIMEOUT,
        ttl: timedelta = timedelta(seconds=60),
        restart_window: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_workers: int = 10,
        cache_ttl: timedelta = DEFAULT_PROBE_CACHE_TTL,
    ) -> None:
        self._kube_list = kube_list
        self._http_get_json = http_get_json
        self._dagster_query = dagster_query
        self._store = store
        self._archive_store = archive_store
        self._spool_read = spool_read
        self._archive_thresholds = archive_thresholds
        self._timeout = timeout
        self._ttl = ttl
        self._restart_window = restart_window
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[datetime, list[Signal]]] = {}
        self._cache_lock = threading.Lock()
        # One pool for the life of this object. A probe stuck in a socket cannot be cancelled and keeps its worker, so
        # the threads a hung dependency can hold are capped at `max_workers`; probes beyond that queue and read as
        # "probe timed out" (unknown) until a worker frees up. Size it for the stages probed at once (5 per stage).
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="service-probe")

    @classmethod
    def in_cluster(
        cls, store: ListableStore | None, archive_store: ListableStore | None = None, **options: Any
    ) -> ServiceProbes:
        """Probes against the pod's own cluster; every call is bounded by the probe `timeout` (default 5 seconds).

        `store` is the S3 client the object-store probe lists with. This class cannot bound a call made inside
        boto3, so build the client with `bounded_s3_config(timeout)` (connect and read timeouts no larger than the
        probe timeout, one attempt); a client with larger timeouts is refused here rather than left to pile up threads.
        """
        timeout = options.get("timeout", PROBE_TIMEOUT)
        seconds = timeout.total_seconds()
        _require_bounded_store(store, seconds)
        _require_bounded_store(archive_store, seconds)
        return cls(
            kube_list=in_cluster_kube_list(seconds),
            http_get_json=_http_get_json,
            dagster_query=_dagster_query(seconds),
            store=store,
            archive_store=archive_store,
            spool_read=in_cluster_spool_read(seconds),
            **options,
        )

    def close(self) -> None:
        """Stop accepting probes. A probe stuck in a socket is abandoned, not joined, so shutdown never waits on one."""
        self._pool.shutdown(wait=False, cancel_futures=True)

    def probe(self, targets: StageTargets) -> list[Signal]:
        """Signals for one stage, reused for `cache_ttl`; a probe that raised is never cached."""
        with self._cache_lock:
            cached = self._cache.get(targets.stage)
            if cached and self._clock() - cached[0] < self._cache_ttl:
                return list(cached[1])
        signals = self._probe_uncached(targets)
        with self._cache_lock:
            self._cache[targets.stage] = (self._clock(), signals)
        return list(signals)

    def _probe_uncached(self, targets: StageTargets) -> list[Signal]:
        """The probes run concurrently and share one timeout."""
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
            ("polaris.readiness", targets.polaris_health_url is not None, lambda: self._polaris(targets)),
            (
                "object-store.list",
                targets.bucket is not None and self._store is not None,
                lambda: self._object_store(targets),
            ),
        ]
        spool = _Once(lambda: self._read_spool(targets))
        archive_checks: list[tuple[str, bool, Callable[[], _Outcome]]] = [
            (
                SPOOL_AGE_COMPONENT,
                self._spool_enabled(targets),
                lambda: self._spool_outcome(targets, spool.get(), evaluate_spool_age),
            ),
            (
                SPOOL_SIZE_COMPONENT,
                self._spool_enabled(targets),
                lambda: self._spool_outcome(targets, spool.get(), evaluate_spool_size),
            ),
            (
                NEWEST_OBJECT_COMPONENT,
                targets.log_archive_base_uri is not None
                and targets.log_archive_namespace is not None
                and self._archive_store is not None,
                lambda: self._newest_object(targets),
            ),
        ]
        checks.extend(archive_checks)
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

    def _run(self, checks: list[tuple[str, Callable[[], _Outcome]]]) -> dict[str, _Outcome]:
        # A probe stuck in a socket cannot be cancelled, so it is abandoned rather than joined. A probe that never got
        # a worker is cancelled so it does not run late.
        deadline = monotonic() + self._timeout.total_seconds()
        futures = {name: self._pool.submit(check) for name, check in checks}
        results: dict[str, _Outcome] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=max(0.0, deadline - monotonic()))
            except FutureTimeout:
                future.cancel()
                results[name] = _Outcome(HealthState.UNKNOWN, "probe timed out")
            except ProbeError as exc:
                results[name] = _Outcome(HealthState.UNKNOWN, str(exc))
            except Exception as exc:  # noqa: BLE001 - a probe bug must degrade one signal, not the whole answer
                log.warning("service probe %s failed unexpectedly", name, exc_info=True)
                results[name] = _Outcome(HealthState.UNKNOWN, f"probe failed: {type(exc).__name__}")
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
            # Never the exception text: GraphQL error payloads carry messages and stack extensions.
            log.warning("Dagster probe failed", exc_info=True)
            raise ProbeError(f"Dagster did not answer ({_cause(exc)})") from exc
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
            log.warning("Trino probe failed", exc_info=True)
            raise ProbeError(f"Trino did not answer ({_cause(exc)})") from exc
        if info.get("starting") is True:
            return _Outcome(HealthState.WARNING, "Trino is still starting")
        if info.get("starting") is False:
            return _Outcome(HealthState.HEALTHY, "Trino coordinator has finished starting")
        raise ProbeError("Trino's answer did not say whether it is starting")

    def _polaris(self, targets: StageTargets) -> _Outcome:
        assert targets.polaris_health_url is not None
        url = f"{targets.polaris_health_url.rstrip('/')}/q/health/ready"
        try:
            answer = self._http_get_json(url, self._timeout.total_seconds())
        except requests.HTTPError as exc:
            # Quarkus answers 503 with a body whose status is DOWN when a readiness check fails; any other refusal is
            # not Polaris speaking (a proxy, a wrong port), so it says nothing about the catalog.
            response = exc.response
            if response is not None and response.status_code == _NOT_READY and _body_status(response) == "DOWN":
                return _Outcome(HealthState.CRITICAL, "Polaris reports itself not ready")
            log.warning("Polaris probe failed", exc_info=True)
            raise ProbeError(f"Polaris did not answer ({_cause(exc)})") from exc
        except (requests.RequestException, ValueError) as exc:
            log.warning("Polaris probe failed", exc_info=True)
            raise ProbeError(f"Polaris did not answer ({_cause(exc)})") from exc
        status = answer.get("status")
        if status == "UP":
            return _Outcome(HealthState.HEALTHY, "Polaris answers its readiness check")
        if status == "DOWN":
            return _Outcome(HealthState.CRITICAL, "Polaris reports itself not ready")
        raise ProbeError("Polaris's answer did not say whether it is ready")

    def _spool_enabled(self, targets: StageTargets) -> bool:
        return targets.spool_probe_url is not None and self._spool_read is not None

    def _read_spool(self, targets: StageTargets) -> tuple[list[SpoolObservation], int]:
        assert self._spool_read is not None and targets.spool_probe_url is not None
        return self._spool_read(targets.spool_probe_url)

    def _spool_outcome(
        self,
        targets: StageTargets,
        read: tuple[list[SpoolObservation], int] | BaseException,
        evaluate: Callable[..., Signal],
    ) -> _Outcome:
        """The worst of the stage's spool over every collector pod; a pod that did not answer is unknown."""
        if isinstance(read, BaseException):
            raise read
        observations, unanswered = read
        now = self._clock()
        mine = [o for o in observations if o.stage == targets.stage]
        signals = [
            evaluate(targets.stage, o, now=now, max_age=self._ttl, thresholds=self._archive_thresholds) for o in mine
        ]
        if unanswered:
            noun = "pod" if unanswered == 1 else "pods"
            signals.append(
                service_signal(
                    Scope(stage=targets.stage),
                    "log-archive",
                    HealthState.UNKNOWN,
                    f"{unanswered} collector {noun} did not answer",
                    now=now,
                    max_age=self._ttl,
                )
            )
        if not signals:
            raise ProbeError("no collector pod reported this stage's spool")
        worst = worst_state(signal.state for signal in signals)
        reason = next(signal.reason for signal in signals if signal.state is worst)
        return _Outcome(worst, reason)

    def _newest_object(self, targets: StageTargets) -> _Outcome:
        assert self._archive_store is not None
        assert targets.log_archive_base_uri is not None and targets.log_archive_namespace is not None
        bucket, _, prefix = targets.log_archive_base_uri.removeprefix("s3://").partition("/")
        now = self._clock()
        newest: datetime | None = None
        for back in range(ARCHIVE_DAYS):
            day = (now - timedelta(days=back)).date().isoformat()
            listed = self._newest_under(
                bucket, f"{prefix.strip('/')}/v2/namespace={targets.log_archive_namespace}/date={day}/"
            )
            if listed is not None and (newest is None or listed > newest):
                newest = listed
        signal = evaluate_newest_object(
            targets.stage,
            None if newest is None else max(timedelta(0), now - newest),
            observed_at=now,
            now=now,
            max_age=self._ttl,
            thresholds=self._archive_thresholds,
        )
        return _Outcome(signal.state, signal.reason)

    def _newest_under(self, bucket: str, prefix: str) -> datetime | None:
        assert self._archive_store is not None
        newest: datetime | None = None
        token: str | None = None
        for _ in range(_ARCHIVE_PAGES):
            args: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                args["ContinuationToken"] = token
            try:
                page = self._archive_store.list_objects_v2(**args)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                raise ProbeError(f"the object store refused the archive listing ({code or 'unknown error'})") from exc
            except BotoCoreError as exc:
                raise ProbeError(f"the object store is unreachable: {type(exc).__name__}") from exc
            for item in page.get("Contents", []):
                modified = item["LastModified"]
                if newest is None or modified > newest:
                    newest = modified
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                break
        return newest

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


SPOOL_AGE_COMPONENT = "log-archive.spool-age"
SPOOL_SIZE_COMPONENT = "log-archive.spool-size"
NEWEST_OBJECT_COMPONENT = "log-archive.newest-object"


# What each component's probe asks, shown beside its answer. A component not listed reads `platform-api`.
COMPONENT_SOURCES = {
    "dagster.workloads": "kubernetes",
    "dagster.read-api": "dagster-graphql",
    "trino.workloads": "kubernetes",
    "trino.readiness": "trino-http",
    "polaris.readiness": "polaris-http",
    "object-store.list": "s3",
    SPOOL_AGE_COMPONENT: "log-collector",
    SPOOL_SIZE_COMPONENT: "log-collector",
    NEWEST_OBJECT_COMPONENT: "s3",
}


class _Once:
    """Runs `read` at most once however many checks ask, and hands every one the same answer or the same failure."""

    def __init__(self, read: Callable[[], tuple[list[SpoolObservation], int]]) -> None:
        self._read = read
        self._lock = threading.Lock()
        self._done = False
        self._value: tuple[list[SpoolObservation], int] | BaseException | None = None

    def get(self) -> tuple[list[SpoolObservation], int] | BaseException:
        with self._lock:
            if not self._done:
                try:
                    self._value = self._read()
                except Exception as exc:  # noqa: BLE001 - re-raised by each caller, where it becomes `unknown`
                    self._value = exc
                self._done = True
            assert self._value is not None
            return self._value


# The probes run together and share the check's deadline (`_run`). Each pod gets this fraction of it, so a pod that
# hangs is given up on while the check still has time to report what the others said.
_SPOOL_POD_BUDGET = 0.6


def in_cluster_spool_read(timeout: float) -> SpoolRead:
    """Ask every pod behind the headless probe Service at once, giving each `_SPOOL_POD_BUDGET` of `timeout`.

    A pod that fails or does not answer in time counts as unanswered; the answers of the others are kept, so a
    reachable collector's warning or critical reading is not lost to a silent neighbour.
    """
    per_pod = timeout * _SPOOL_POD_BUDGET

    def fetch(url: str) -> list[SpoolObservation]:
        # Connect and read are separate timeouts, so each gets half to keep the call within the pod's budget.
        response = requests.get(url, timeout=(per_pod / 2, per_pod / 2))
        response.raise_for_status()
        return parse_spool_report(response.json())

    def read(base_url: str) -> tuple[list[SpoolObservation], int]:
        parts = urlsplit(base_url)
        try:
            addresses = sorted(
                {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port, type=socket.SOCK_STREAM)}
            )
        except OSError as exc:
            raise ProbeError(f"the collector probe Service did not resolve: {type(exc).__name__}") from exc
        if not addresses:
            raise ProbeError("the collector probe Service has no ready pod")
        # One worker per pod, so no pod waits for another's worker. A hung socket cannot be cancelled: the pool is
        # abandoned, not joined, and the thread ends when its own timeout fires.
        pool = ThreadPoolExecutor(max_workers=len(addresses), thread_name_prefix="spool-read")
        deadline = monotonic() + per_pod
        futures = {
            address: pool.submit(
                fetch,
                f"{parts.scheme}://{f'[{address}]' if ':' in str(address) else address}:{parts.port}{SPOOL_PATH}",
            )
            for address in addresses
        }
        observations: list[SpoolObservation] = []
        unanswered = 0
        try:
            for address, future in futures.items():
                try:
                    observations.extend(future.result(timeout=max(0.0, deadline - monotonic())))
                except (FutureTimeout, requests.RequestException, ValueError):
                    log.warning("collector spool probe at %s failed", address, exc_info=True)
                    unanswered += 1
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return observations, unanswered

    return read


def _cause(exc: BaseException) -> str:
    """A fixed, body-free description of a failure for a signal reason: the HTTP status, else the exception type."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return f"HTTP {status}" if isinstance(status, int) else type(exc).__name__


def _body_status(response: requests.Response) -> object:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("status") if isinstance(body, dict) else None


def bounded_s3_config(timeout: timedelta) -> Config:
    """The botocore Config for the object-store client: one attempt, connect and read bounded by the probe timeout."""
    seconds = timeout.total_seconds()
    return Config(connect_timeout=seconds, read_timeout=seconds, retries={"max_attempts": 1})


def _require_bounded_store(store: ListableStore | None, seconds: float) -> None:
    config = getattr(getattr(store, "meta", None), "config", None)
    for name in ("connect_timeout", "read_timeout"):
        value = getattr(config, name, None)
        if config is not None and (not isinstance(value, int | float) or value > seconds):
            raise ValueError(f"the object-store client's {name} must be at most the probe timeout ({seconds}s)")


def _dagster_query(timeout: float) -> DagsterQuery:
    """GraphQL through DagsterClient's `request_json` hook, so the post carries the probe timeout, not its 30s."""

    def query(url: str, document: str) -> Mapping[str, Any]:
        def post(text: str, variables: Mapping[str, Any] | None) -> Mapping[str, Any]:
            response = requests.post(
                url,
                json={"query": text, "variables": variables or {}},
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            response.raise_for_status()
            return dict(response.json())

        return DagsterClient(url, request_json=post).graphql(document)

    return query


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


def in_cluster_kube_list(timeout: float = 5.0) -> KubeList:
    """GET against the API server as the pod's ServiceAccount; a 401 or 403 is a ProbeError, never a guess.

    `timeout` is the whole probe's budget. A workload probe makes three sequential calls, so each is bounded to a
    third of it and the three together fit.
    """
    per_call = timeout / _CALLS_PER_WORKLOAD_PROBE

    def kube_list(path: str) -> Mapping[str, Any]:
        try:
            # Read per call: the kubelet rotates the projected token.
            token = (_SERVICE_ACCOUNT / "token").read_text().strip()
            response = requests.get(
                f"{_API_SERVER}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                verify=str(_SERVICE_ACCOUNT / "ca.crt"),
                timeout=per_call,
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
