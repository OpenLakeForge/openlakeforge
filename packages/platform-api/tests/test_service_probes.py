"""Service probes driven by cluster JSON, HTTP answers and an object store fixture; none restates the adapter."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from botocore.exceptions import ClientError, EndpointConnectionError
from openlakeforge_core import HealthState, Signal, SignalKind, SpoolObservation
from openlakeforge_services.dagster import DagsterTransientError

from openlakeforge_platform_api import service_probes
from openlakeforge_platform_api.service_probes import (
    ProbeError,
    ServiceProbes,
    StageTargets,
    bounded_s3_config,
    in_cluster_kube_list,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)
DAGSTER = {"app.kubernetes.io/name": "dagster"}
TRINO = {"app.kubernetes.io/name": "trino"}
ALL = StageTargets(
    stage="dev",
    namespace="olf-dev",
    dagster_graphql_url="http://dagster.olf-dev/graphql",
    trino_url="http://trino.olf-dev:8080",
    bucket="ops",
    log_archive_base_uri="s3://ops/activations/dev/logs",
    log_archive_namespace="olf-dev",
    spool_probe_url="http://log-collector-probe.olf-system.svc:2021",
)
REPOSITORIES = {"repositoriesOrError": {"__typename": "RepositoryConnection", "nodes": [{"name": "r"}]}}


def controller(labels: Mapping[str, str], *, replicas: int = 1, ready: int | None = None) -> dict[str, Any]:
    return {
        "spec": {"replicas": replicas, "selector": {"matchLabels": dict(labels)}},
        "status": {"readyReplicas": replicas if ready is None else ready},
    }


def pod(
    labels: Mapping[str, str],
    *,
    restarts: int = 0,
    last_died: timedelta = timedelta(minutes=1),
    waiting: str | None = None,
    unschedulable: bool = False,
) -> dict[str, Any]:
    status: dict[str, Any] = {"containerStatuses": [{"restartCount": restarts, "state": {}}]}
    if restarts:
        finished = (NOW - last_died).isoformat().replace("+00:00", "Z")
        status["containerStatuses"][0]["lastState"] = {"terminated": {"finishedAt": finished}}
    if waiting:
        status["containerStatuses"][0]["state"] = {"waiting": {"reason": waiting}}
    if unschedulable:
        status["phase"] = "Pending"
        status["conditions"] = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]
    return {"metadata": {"labels": dict(labels)}, "status": status}


class Cluster:
    """A namespace's controllers and pods, answering the list calls the adapter makes."""

    def __init__(
        self,
        controllers: list[dict[str, Any]],
        pods: list[dict[str, Any]],
        statefulsets: list[dict[str, Any]] | None = None,
    ) -> None:
        self.controllers = controllers
        self.statefulsets = statefulsets or []
        self.pods = pods
        self.denied = False

    def __call__(self, path: str) -> Mapping[str, Any]:
        if self.denied:
            raise ProbeError("the Kubernetes API refused the read (HTTP 403)")
        if "/pods" in path:
            return {"items": self.pods}
        if "/deployments" in path or "/statefulsets" in path:
            key, value = path.split("labelSelector=")[1].replace("%2F", "/").split("%3D")
            source = self.controllers if "/deployments" in path else self.statefulsets
            return {"items": [c for c in source if c["spec"]["selector"]["matchLabels"].get(key) == value]}
        return {"items": []}


class Store:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def list_objects_v2(self, **_: Any) -> Any:
        if self.error:
            raise self.error
        return {"KeyCount": 0}


class Archive:
    """The archive listing: objects by key, each with the time it was last modified."""

    def __init__(self, modified: dict[str, datetime] | None = None, error: Exception | None = None) -> None:
        self.modified = modified or {}
        self.error = error
        self.prefixes: list[str] = []

    def list_objects_v2(self, **kwargs: Any) -> Any:
        if self.error:
            raise self.error
        self.prefixes.append(kwargs["Prefix"])
        contents = [{"Key": k, "LastModified": t} for k, t in self.modified.items() if k.startswith(kwargs["Prefix"])]
        return {"Contents": contents, "IsTruncated": False}


ARCHIVE_PREFIX = "activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/"


def fresh_archive(age: timedelta = timedelta(minutes=2)) -> Archive:
    return Archive({ARCHIVE_PREFIX + "dev-1.ndjson.gz": NOW - age})


def spool(*, age: timedelta | None = None, size: int = 0, stage: str = "dev", seen: datetime = NOW) -> SpoolObservation:
    return SpoolObservation(stage, seen, age, size, 512_000_000)


def healthy_cluster() -> Cluster:
    return Cluster([controller(DAGSTER), controller(TRINO)], [pod(DAGSTER), pod(TRINO)])


def probes(
    cluster: Callable[[str], Mapping[str, Any]] | None = None,
    *,
    info: Callable[[], Mapping[str, Any]] = lambda: {"starting": False},
    dagster: Callable[[], Mapping[str, Any]] = lambda: REPOSITORIES,
    store: Store | None = None,
    archive: Archive | None = None,
    spool_read: Callable[[str], tuple[list[SpoolObservation], int]] = lambda url: ([spool()], 0),
    timeout: timedelta = timedelta(seconds=2),
) -> ServiceProbes:
    return ServiceProbes(
        kube_list=cluster or healthy_cluster(),
        http_get_json=lambda url, _timeout: info(),
        dagster_query=lambda url, query: dagster(),
        store=store or Store(),
        archive_store=archive or fresh_archive(),
        spool_read=spool_read,
        timeout=timeout,
        ttl=TTL,
        clock=lambda: NOW,
    )


def by_component(signals: list[Signal]) -> dict[str, Signal]:
    return {str(signal.component): signal for signal in signals}


def test_a_healthy_stage_is_healthy_everywhere_with_observed_at_and_expiry() -> None:
    signals = probes().probe(ALL)
    assert {s.state for s in signals} == {HealthState.HEALTHY}
    assert {s.kind for s in signals} == {SignalKind.SERVICE}
    assert all(s.stage == "dev" and s.observed_at == NOW and s.expires_at == NOW + TTL for s in signals)


def test_a_killed_workload_is_critical_and_a_partial_one_is_a_warning() -> None:
    killed = Cluster([controller(DAGSTER, ready=0), controller(TRINO)], [pod(TRINO)])
    partial = Cluster([controller(DAGSTER), controller(TRINO, replicas=2, ready=1)], [pod(DAGSTER), pod(TRINO)])
    assert by_component(probes(killed).probe(ALL))["dagster.workloads"].state is HealthState.CRITICAL
    assert by_component(probes(partial).probe(ALL))["trino.workloads"].state is HealthState.WARNING


def test_a_stuck_pod_is_degraded_whether_unschedulable_or_crash_looping() -> None:
    stuck = Cluster(
        [controller(DAGSTER, ready=0), controller(TRINO)],
        [pod(DAGSTER, unschedulable=True), pod(TRINO, waiting="CrashLoopBackOff", restarts=7)],
    )
    signals = by_component(probes(stuck).probe(ALL))
    assert signals["dagster.workloads"].state is HealthState.CRITICAL
    assert "unschedulable" in signals["dagster.workloads"].reason
    assert signals["trino.workloads"].state is HealthState.WARNING


def test_a_pod_that_restarted_repeatedly_just_now_is_not_green() -> None:
    flapping = Cluster([controller(DAGSTER), controller(TRINO)], [pod(DAGSTER), pod(TRINO, restarts=5)])
    assert by_component(probes(flapping).probe(ALL))["trino.workloads"].state is HealthState.WARNING


def test_restarts_that_ended_before_the_window_do_not_keep_a_settled_pod_on_warning() -> None:
    settled = Cluster(
        [controller(DAGSTER), controller(TRINO)],
        [pod(DAGSTER), pod(TRINO, restarts=40, last_died=timedelta(hours=6))],
    )
    assert {s.state for s in probes(settled).probe(ALL)} == {HealthState.HEALTHY}


def test_the_restart_window_is_configurable() -> None:
    cluster = Cluster(
        [controller(DAGSTER), controller(TRINO)], [pod(DAGSTER), pod(TRINO, restarts=5, last_died=timedelta(hours=2))]
    )
    wide = ServiceProbes(
        kube_list=cluster,
        http_get_json=lambda url, _timeout: {"starting": False},
        dagster_query=lambda url, query: REPOSITORIES,
        store=Store(),
        restart_window=timedelta(hours=3),
        ttl=TTL,
        clock=lambda: NOW,
    )
    assert by_component(wide.probe(ALL))["trino.workloads"].state is HealthState.WARNING
    assert by_component(probes(cluster).probe(ALL))["trino.workloads"].state is HealthState.HEALTHY


def test_a_namespace_denying_the_read_gives_unknown_not_healthy() -> None:
    cluster = healthy_cluster()
    cluster.denied = True
    signals = by_component(probes(cluster).probe(ALL))
    assert signals["dagster.workloads"].state is HealthState.UNKNOWN
    assert signals["trino.workloads"].state is HealthState.UNKNOWN
    assert signals["trino.readiness"].state is HealthState.HEALTHY


def test_a_selector_matching_no_controller_gives_unknown_not_the_scaled_to_zero_warning() -> None:
    signals = by_component(probes(Cluster([], [])).probe(ALL))
    assert signals["dagster.workloads"].state is HealthState.UNKNOWN
    assert "matches" in signals["dagster.workloads"].reason


def test_a_controller_scaled_to_zero_is_a_warning() -> None:
    cluster = Cluster([controller(DAGSTER, replicas=0), controller(TRINO)], [pod(TRINO)])
    assert by_component(probes(cluster).probe(ALL))["dagster.workloads"].state is HealthState.WARNING


def test_a_probe_that_never_answers_times_out_to_unknown_without_holding_the_others() -> None:
    release = threading.Event()

    def hang() -> Mapping[str, Any]:
        release.wait(5)
        return {"starting": False}

    try:
        signals = by_component(probes(info=hang, timeout=timedelta(milliseconds=100)).probe(ALL))
    finally:
        release.set()
    assert signals["trino.readiness"].state is HealthState.UNKNOWN
    assert signals["trino.readiness"].reason == "probe timed out"
    assert signals["dagster.workloads"].state is HealthState.HEALTHY


def test_a_stage_without_dagster_trino_or_a_store_reports_disabled_and_probes_nothing() -> None:
    def forbidden(path: str) -> Mapping[str, Any]:
        raise AssertionError("a disabled stage must not touch the cluster")

    signals = probes(forbidden).probe(StageTargets(stage="dev", namespace="olf-dev"))
    assert {s.state for s in signals} == {HealthState.DISABLED}
    assert {s.component for s in signals} == {
        "dagster.workloads",
        "dagster.read-api",
        "trino.workloads",
        "trino.readiness",
        "object-store.list",
        "log-archive.spool-age",
        "log-archive.spool-size",
        "log-archive.newest-object",
    }


def test_disabling_only_trino_leaves_dagster_probed() -> None:
    targets = StageTargets(stage="dev", namespace="olf-dev", dagster_graphql_url="http://d/graphql")
    signals = by_component(probes().probe(targets))
    assert signals["trino.readiness"].state is HealthState.DISABLED
    assert signals["dagster.read-api"].state is HealthState.HEALTHY


def test_trino_that_is_still_starting_is_a_warning_and_one_that_is_down_is_unknown() -> None:
    starting = by_component(probes(info=lambda: {"starting": True}).probe(ALL))
    assert starting["trino.readiness"].state is HealthState.WARNING

    def down() -> Mapping[str, Any]:
        raise requests.ConnectionError("refused")

    assert by_component(probes(info=down).probe(ALL))["trino.readiness"].state is HealthState.UNKNOWN


def test_trino_answering_something_that_is_not_its_info_document_is_unknown() -> None:
    signals = by_component(probes(info=lambda: {"html": "login"}).probe(ALL))
    assert signals["trino.readiness"].state is HealthState.UNKNOWN


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"repositoriesOrError": {"__typename": "RepositoryConnection", "nodes": []}}, HealthState.WARNING),
        ({"repositoriesOrError": {"__typename": "PythonError", "message": "boom"}}, HealthState.CRITICAL),
        ({"repositoriesOrError": None}, HealthState.UNKNOWN),
    ],
)
def test_dagster_answering_200_with_a_bad_workspace_is_not_healthy(
    answer: dict[str, Any], expected: HealthState
) -> None:
    assert by_component(probes(dagster=lambda: answer).probe(ALL))["dagster.read-api"].state is expected


def test_dagster_transport_failure_is_unknown() -> None:
    def down() -> Mapping[str, Any]:
        raise DagsterTransientError("connection refused")

    assert by_component(probes(dagster=down).probe(ALL))["dagster.read-api"].state is HealthState.UNKNOWN


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ClientError({"Error": {"Code": "NoSuchBucket"}}, "ListObjectsV2"), HealthState.CRITICAL),
        (ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2"), HealthState.UNKNOWN),
        (EndpointConnectionError(endpoint_url="http://s3"), HealthState.UNKNOWN),
        (None, HealthState.HEALTHY),
    ],
)
def test_object_store_outcomes(error: Exception | None, expected: HealthState) -> None:
    assert by_component(probes(store=Store(error)).probe(ALL))["object-store.list"].state is expected


def test_a_statefulset_is_observed_like_a_deployment() -> None:
    cluster = Cluster([controller(DAGSTER)], [pod(DAGSTER), pod(TRINO)], statefulsets=[controller(TRINO, ready=0)])
    assert by_component(probes(cluster).probe(ALL))["trino.workloads"].state is HealthState.CRITICAL
    healthy = Cluster([controller(DAGSTER)], [pod(DAGSTER), pod(TRINO)], statefulsets=[controller(TRINO)])
    assert by_component(probes(healthy).probe(ALL))["trino.workloads"].state is HealthState.HEALTHY


def test_a_dagster_errors_payload_never_reaches_the_signal_reason() -> None:
    class Response:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {"errors": [{"message": "secret_token=abc", "extensions": {"stack": ["secret_token=abc"]}}]}

    seen: dict[str, Any] = {}

    def post(url: str, **kwargs: Any) -> Response:
        seen.update(kwargs)
        return Response()

    query = service_probes._dagster_query(2.0)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service_probes.requests, "post", post)
        probe = ServiceProbes(
            kube_list=healthy_cluster(),
            http_get_json=lambda url, _t: {"starting": False},
            dagster_query=query,
            store=Store(),
            ttl=TTL,
            clock=lambda: NOW,
        )
        signal = by_component(probe.probe(ALL))["dagster.read-api"]
    assert signal.state is HealthState.UNKNOWN
    assert "secret_token" not in signal.reason and "abc" not in signal.reason
    assert signal.reason == "Dagster did not answer (ServiceClientError)"
    assert seen["timeout"] == 2.0


def test_error_text_from_trino_is_not_echoed_either() -> None:
    def leaky() -> Mapping[str, Any]:
        raise requests.HTTPError("503 body: secret_token=abc")

    signal = by_component(probes(info=leaky).probe(ALL))["trino.readiness"]
    assert signal.state is HealthState.UNKNOWN
    assert "secret_token" not in signal.reason


def test_the_dagster_probe_reports_the_http_status_not_the_body() -> None:
    def refused() -> Mapping[str, Any]:
        response = requests.Response()
        response.status_code = 502
        raise requests.HTTPError("secret_token=abc", response=response)

    assert (
        by_component(probes(dagster=refused).probe(ALL))["dagster.read-api"].reason
        == "Dagster did not answer (HTTP 502)"
    )


def test_hung_probes_hold_a_bounded_number_of_threads_across_many_calls() -> None:
    release = threading.Event()
    started = 0

    def hang() -> Mapping[str, Any]:
        nonlocal started
        started += 1
        release.wait(5)
        return {"starting": False}

    hung = ServiceProbes(
        kube_list=healthy_cluster(),
        http_get_json=lambda url, _t: hang(),
        dagster_query=lambda url, query: REPOSITORIES,
        store=Store(),
        timeout=timedelta(milliseconds=50),
        ttl=TTL,
        clock=lambda: NOW,
        max_workers=2,
    )
    try:
        for _ in range(5):
            assert by_component(hung.probe(ALL))["trino.readiness"].state is HealthState.UNKNOWN
        # Each call's hung probe would be a new thread under a pool-per-call; here workers are reused, so at most 2.
        assert started <= 2
    finally:
        release.set()


def test_the_s3_config_and_in_cluster_refuse_an_unbounded_store() -> None:
    config = bounded_s3_config(timedelta(seconds=5))
    assert (vars(config)["connect_timeout"], vars(config)["read_timeout"], vars(config)["retries"]) == (
        5.0,
        5.0,
        {"max_attempts": 1},
    )

    class Meta:
        def __init__(self, seconds: float) -> None:
            self.config = bounded_s3_config(timedelta(seconds=seconds))

    class Client(Store):
        def __init__(self, seconds: float) -> None:
            super().__init__()
            self.meta = Meta(seconds)

    ServiceProbes.in_cluster(Client(5))
    with pytest.raises(ValueError, match="connect_timeout"):
        ServiceProbes.in_cluster(Client(60))


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self.ok = status < 400
        self._body = body or {"items": []}

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture
def service_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "token").write_text("tok\n")
    (tmp_path / "ca.crt").write_text("ca")
    monkeypatch.setattr(service_probes, "_SERVICE_ACCOUNT", tmp_path)
    return tmp_path


def test_the_kubernetes_client_sends_the_token_and_the_in_cluster_ca_and_reads_only_three_lists(
    service_account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"items": [controller(DAGSTER)]} if "deployments" in url else None)

    monkeypatch.setattr(service_probes.requests, "get", get)
    kube = in_cluster_kube_list(6.0)
    service_probes.observe_workloads(kube, "olf-dev", DAGSTER, observed_at=NOW, restart_window=timedelta(minutes=30))
    prefix = "https://kubernetes.default.svc/"
    assert [c["url"].removeprefix(prefix).split("?")[0] for c in calls] == [
        "apis/apps/v1/namespaces/olf-dev/deployments",
        "apis/apps/v1/namespaces/olf-dev/statefulsets",
        "api/v1/namespaces/olf-dev/pods",
    ]
    assert all(c["verify"] == str(service_account / "ca.crt") for c in calls)
    assert all(c["headers"]["Authorization"] == "Bearer tok" for c in calls)
    assert all(c["timeout"] == 2.0 for c in calls)  # three calls fit the 6s budget


@pytest.mark.parametrize("status", [401, 403])
def test_the_kubernetes_client_turns_a_refused_read_into_unknown(
    status: int, service_account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_probes.requests, "get", lambda url, **_: FakeResponse(status))
    probe = ServiceProbes(
        kube_list=in_cluster_kube_list(),
        http_get_json=lambda url, _t: {"starting": False},
        dagster_query=lambda url, query: REPOSITORIES,
        store=Store(),
        ttl=TTL,
        clock=lambda: NOW,
    )
    signal = by_component(probe.probe(ALL))["dagster.workloads"]
    assert signal.state is HealthState.UNKNOWN
    assert f"HTTP {status}" in signal.reason


def test_a_missing_token_file_is_unknown_and_never_calls_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_probes, "_SERVICE_ACCOUNT", tmp_path)

    def get(url: str, **_: Any) -> FakeResponse:
        raise AssertionError("no token means no request")

    monkeypatch.setattr(service_probes.requests, "get", get)
    with pytest.raises(ProbeError, match="unreachable"):
        in_cluster_kube_list()("/api/v1/namespaces/olf-dev/pods")


class Ticking:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def cached_probes(clock: Ticking, calls: list[int]) -> ServiceProbes:
    def info() -> Mapping[str, Any]:
        calls.append(1)
        return {"starting": False}

    return ServiceProbes(
        kube_list=healthy_cluster(),
        http_get_json=lambda url, _timeout: info(),
        dagster_query=lambda url, query: REPOSITORIES,
        store=Store(),
        ttl=TTL,
        clock=clock,
        cache_ttl=timedelta(seconds=10),
    )


def test_a_stage_probed_again_within_the_cache_ttl_is_not_re_probed_until_it_lapses() -> None:
    clock, calls = Ticking(), list[int]()
    cached = cached_probes(clock, calls)

    first = cached.probe(ALL)
    clock.now = NOW + timedelta(seconds=9)
    assert cached.probe(ALL) == first
    assert len(calls) == 1

    clock.now = NOW + timedelta(seconds=11)
    cached.probe(ALL)
    assert len(calls) == 2


def test_a_probe_that_raises_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    clock, calls = Ticking(), list[int]()
    cached = cached_probes(clock, calls)
    attempts = []

    def flaky(targets: StageTargets) -> list[Signal]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(cached, "_probe_uncached", flaky)
    with pytest.raises(RuntimeError):
        cached.probe(ALL)
    assert cached.probe(ALL) == []
    assert len(attempts) == 2


def test_a_collector_whose_uploads_are_failing_is_seen_through_its_spool_and_the_archive() -> None:
    old = spool(age=timedelta(minutes=12), size=300_000_000)
    stale = fresh_archive(timedelta(minutes=20))

    signals = by_component(probes(spool_read=lambda url: ([old], 0), archive=stale).probe(ALL))

    assert signals["log-archive.spool-age"].state is HealthState.WARNING
    assert signals["log-archive.spool-size"].state is HealthState.WARNING
    assert signals["log-archive.newest-object"].state is HealthState.WARNING


def test_a_full_spool_and_an_hour_without_an_object_are_critical() -> None:
    full = spool(age=timedelta(minutes=45), size=500_000_000)

    signals = by_component(
        probes(spool_read=lambda url: ([full], 0), archive=fresh_archive(timedelta(hours=2))).probe(ALL)
    )

    assert {
        signals[c].state for c in ("log-archive.spool-age", "log-archive.spool-size", "log-archive.newest-object")
    } == {HealthState.CRITICAL}


def test_the_worst_collector_pod_decides_and_a_silent_one_is_unknown() -> None:
    healthy, stuck = spool(), spool(age=timedelta(minutes=40))

    worst = by_component(probes(spool_read=lambda url: ([healthy, stuck], 0)).probe(ALL))
    silent = by_component(probes(spool_read=lambda url: ([healthy], 1)).probe(ALL))

    assert worst["log-archive.spool-age"].state is HealthState.CRITICAL
    assert silent["log-archive.spool-age"].state is HealthState.UNKNOWN
    assert "did not answer" in silent["log-archive.spool-age"].reason


def test_another_stages_spool_is_not_this_stages() -> None:
    other = spool(stage="prod", age=timedelta(hours=3), size=512_000_000)

    signals = by_component(probes(spool_read=lambda url: ([other], 0)).probe(ALL))

    assert signals["log-archive.spool-age"].state is HealthState.UNKNOWN


def test_a_collector_nobody_can_reach_is_unknown_never_healthy() -> None:
    def unreachable(url: str) -> tuple[list[SpoolObservation], int]:
        raise ProbeError("the collector probe Service has no ready pod")

    signals = by_component(probes(spool_read=unreachable).probe(ALL))

    assert signals["log-archive.spool-age"].state is HealthState.UNKNOWN
    assert signals["log-archive.spool-size"].state is HealthState.UNKNOWN


def test_the_spool_is_read_once_for_both_signals() -> None:
    reads: list[str] = []

    def read(url: str) -> tuple[list[SpoolObservation], int]:
        reads.append(url)
        return [spool()], 0

    probes(spool_read=read).probe(ALL)

    assert len(reads) == 1


def test_the_newest_object_looks_at_today_and_yesterday_of_the_stages_own_prefix() -> None:
    archive = Archive(
        {"activations/dev/logs/v2/namespace=olf-dev/date=2026-11-15/dev-9.ndjson.gz": NOW - timedelta(minutes=3)}
    )

    signal = by_component(probes(archive=archive).probe(ALL))["log-archive.newest-object"]

    assert archive.prefixes == [
        "activations/dev/logs/v2/namespace=olf-dev/date=2026-11-16/",
        "activations/dev/logs/v2/namespace=olf-dev/date=2026-11-15/",
    ]
    assert signal.state is HealthState.HEALTHY


def test_no_object_in_the_days_listed_is_unknown_and_a_refused_listing_is_unknown() -> None:
    empty = by_component(probes(archive=Archive()).probe(ALL))["log-archive.newest-object"]
    denied = Archive(error=ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2"))
    refused = by_component(probes(archive=denied).probe(ALL))["log-archive.newest-object"]

    assert empty.state is HealthState.UNKNOWN
    assert refused.state is HealthState.UNKNOWN
    assert "AccessDenied" in refused.reason


def test_a_stage_without_a_collector_probe_or_archive_reports_those_signals_disabled() -> None:
    bare = StageTargets(stage="dev", namespace="olf-dev", bucket="ops")

    signals = by_component(probes().probe(bare))

    for component in ("log-archive.spool-age", "log-archive.spool-size", "log-archive.newest-object"):
        assert signals[component].state is HealthState.DISABLED


def test_a_collector_pod_that_does_not_answer_is_counted_and_makes_the_spool_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_report = {
        "observed_at": NOW.isoformat(),
        "limit_bytes": 512_000_000,
        "stages": {"dev": {"oldest_file_age_seconds": None, "size_bytes": 0}},
    }

    class Answer:
        def __init__(self, ok: bool) -> None:
            self.ok = ok

        def raise_for_status(self) -> None:
            if not self.ok:
                raise requests.ConnectionError("no answer")

        def json(self) -> dict[str, Any]:
            return healthy_report

    def get(url: str, timeout: float) -> Answer:
        return Answer(ok="10.0.0.1" in url)

    def resolve(host: str | None, port: int | None, **_: Any) -> list[Any]:
        return [(2, 1, 6, "", ("10.0.0.1", port)), (2, 1, 6, "", ("10.0.0.2", port))]

    monkeypatch.setattr(service_probes.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(service_probes.requests, "get", get)
    read = service_probes.in_cluster_spool_read(2.0)

    observations, unanswered = read("http://log-collector-probe.olf-system.svc:2021")
    signals = by_component(probes(spool_read=read).probe(ALL))

    assert (len(observations), unanswered) == (1, 1)
    assert signals["log-archive.spool-age"].state is HealthState.UNKNOWN
    assert signals["log-archive.spool-size"].state is HealthState.UNKNOWN
    assert "did not answer" in signals["log-archive.spool-age"].reason


def test_a_collector_that_hangs_does_not_cost_the_reachable_ones_their_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    critical = {
        "observed_at": NOW.isoformat(),
        "limit_bytes": 512_000_000,
        "stages": {"dev": {"oldest_file_age_seconds": 3000.0, "size_bytes": 500_000_000}},
    }

    class Answer:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return critical

    def get(url: str, timeout: Any) -> Answer:
        if "10.0.0.1" in url:
            release.wait(10)
            raise requests.ConnectTimeout("hung")
        return Answer()

    def resolve(host: str | None, port: int | None, **_: Any) -> list[Any]:
        return [(2, 1, 6, "", ("10.0.0.1", port)), (2, 1, 6, "", ("10.0.0.2", port))]

    monkeypatch.setattr(service_probes.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(service_probes.requests, "get", get)
    outer = timedelta(seconds=2)
    started = time.monotonic()
    try:
        signals = by_component(
            probes(spool_read=service_probes.in_cluster_spool_read(outer.total_seconds()), timeout=outer).probe(ALL)
        )
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert signals["log-archive.spool-age"].state is HealthState.CRITICAL
    assert signals["log-archive.spool-size"].state is HealthState.CRITICAL
    assert elapsed < outer.total_seconds()
