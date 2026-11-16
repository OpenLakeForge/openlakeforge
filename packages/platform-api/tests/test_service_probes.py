"""Service probes driven by cluster JSON, HTTP answers and an object store fixture; none restates the adapter."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from botocore.exceptions import ClientError, EndpointConnectionError
from openlakeforge_core import HealthState, Signal, SignalKind
from openlakeforge_services.dagster import DagsterTransientError

from openlakeforge_platform_api.service_probes import ProbeError, ServiceProbes, StageTargets

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

    def __init__(self, controllers: list[dict[str, Any]], pods: list[dict[str, Any]]) -> None:
        self.controllers = controllers
        self.pods = pods
        self.denied = False

    def __call__(self, path: str) -> Mapping[str, Any]:
        if self.denied:
            raise ProbeError("the Kubernetes API refused the read (HTTP 403)")
        if "/pods" in path:
            return {"items": self.pods}
        if "/deployments" in path:
            key, value = path.split("labelSelector=")[1].replace("%2F", "/").split("%3D")
            return {"items": [c for c in self.controllers if c["spec"]["selector"]["matchLabels"].get(key) == value]}
        return {"items": []}


class Store:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def list_objects_v2(self, **_: Any) -> Any:
        if self.error:
            raise self.error
        return {"KeyCount": 0}


def healthy_cluster() -> Cluster:
    return Cluster([controller(DAGSTER), controller(TRINO)], [pod(DAGSTER), pod(TRINO)])


def probes(
    cluster: Callable[[str], Mapping[str, Any]] | None = None,
    *,
    info: Callable[[], Mapping[str, Any]] = lambda: {"starting": False},
    dagster: Callable[[], Mapping[str, Any]] = lambda: REPOSITORIES,
    store: Store | None = None,
    timeout: timedelta = timedelta(seconds=2),
) -> ServiceProbes:
    return ServiceProbes(
        kube_list=cluster or healthy_cluster(),
        http_get_json=lambda url, _timeout: info(),
        dagster_query=lambda url, query: dagster(),
        store=store or Store(),
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
