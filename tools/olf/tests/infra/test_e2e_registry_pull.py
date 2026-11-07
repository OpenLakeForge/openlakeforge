from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e import _registry_pull as registry_pull
from olf.infra.e2e._shell import E2EError

REPOSITORY = "ghcr.io/acme/lakehouse-code"
IMAGE = f"{REPOSITORY}@sha256:{'a' * 64}"
SECRET = "ghcr-pull"


def _deployment(name: str, *, secrets: tuple[str, ...] = (SECRET,), image: str = IMAGE) -> dict[str, Any]:
    pod = {"containers": [{"image": image}], "imagePullSecrets": [{"name": item} for item in secrets]}
    return {"kind": "Deployment", "metadata": {"name": name}, "spec": {"template": {"spec": pod}}}


def _cronjob(*, secrets: tuple[str, ...]) -> dict[str, Any]:
    pod = {"containers": [{"image": IMAGE}], "imagePullSecrets": [{"name": item} for item in secrets]}
    template = {"spec": {"template": {"spec": pod}}}
    return {"kind": "CronJob", "metadata": {"name": "log-archive"}, "spec": {"jobTemplate": template}}


def test_every_kind_of_workload_running_the_image_must_list_the_secret() -> None:
    items = [
        _deployment("code-location"),
        _cronjob(secrets=()),
        {"kind": "Pod", "metadata": {"name": "run-pod"}, "spec": {"containers": [{"image": IMAGE}]}},
        _deployment("postgres", secrets=(), image="docker.io/library/postgres:16"),
    ]

    missing, users = registry_pull.workloads_missing_entry(items, IMAGE, SECRET)

    assert missing == ["CronJob/log-archive", "Pod/run-pod"]
    assert users == 3


def test_a_different_secret_does_not_count() -> None:
    missing, _ = registry_pull.workloads_missing_entry([_deployment("code", secrets=("other",))], IMAGE, SECRET)

    assert missing == ["Deployment/code"]


class _Response:
    def __init__(self, status: int, *, headers: dict[str, str] | None = None, body: dict[str, str] | None = None):
        self.status_code = status
        self.headers = headers or {}
        self.ok = status < 400
        self._body = body or {}

    def json(self) -> dict[str, str]:
        return self._body


class _Registry:
    """An anonymous-token registry: the manifest needs a Bearer token, and `public` decides if one is granted."""

    def __init__(self, *, public: bool) -> None:
        self.public = public

    def get(self, url: str, *, headers: dict[str, str] | None = None, params: Any = None, timeout: int = 0) -> Any:
        if "token" in url:
            return _Response(200, body={"token": "t"}) if self.public else _Response(403)
        if (headers or {}).get("Authorization") == "Bearer t":
            return _Response(200)
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
        return _Response(401, headers={"WWW-Authenticate": challenge})


def test_a_registry_that_grants_an_anonymous_token_is_public() -> None:
    assert registry_pull.anonymous_manifest_status(IMAGE, session=_Registry(public=True)) == 200  # type: ignore[arg-type] # test double
    assert registry_pull.anonymous_manifest_status(IMAGE, session=_Registry(public=False)) == 403  # type: ignore[arg-type] # test double


def test_an_unreachable_registry_fails_the_check_rather_than_passing_it() -> None:
    class Down:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise requests.ConnectionError("no route")

    with pytest.raises(E2EError, match="could not tell whether"):
        registry_pull.anonymous_manifest_status(IMAGE, session=Down())  # type: ignore[arg-type] # test double


def test_the_image_ids_to_remove_are_those_under_the_repository() -> None:
    listing = json.dumps(
        {
            "images": [
                {"id": "sha256:1", "repoTags": [f"{REPOSITORY}:sha256-aaa"], "repoDigests": []},
                {"id": "sha256:2", "repoTags": [], "repoDigests": [f"{REPOSITORY}@sha256:bbb"]},
                {"id": "sha256:3", "repoTags": [f"{REPOSITORY}-other:1"], "repoDigests": []},
                {"id": "sha256:4", "repoTags": ["registry.k8s.io/pause:3.9"]},
            ]
        }
    )

    assert registry_pull.image_ids(listing, REPOSITORY) == ["sha256:1", "sha256:2"]


def _event(pod: str, message: str, reason: str = "Pulled") -> dict[str, Any]:
    return {"involvedObject": {"kind": "Pod", "name": pod}, "reason": reason, "message": message}


def test_a_pod_must_show_a_real_pull_and_no_cache_hit() -> None:
    events = [
        _event("fresh", f'Successfully pulled image "{IMAGE}" in 2s'),
        _event("cached", f'Container image "{IMAGE}" already present on machine'),
        _event("silent", "Started container"),
    ]

    problems = registry_pull.pull_failures(events, ["fresh", "cached", "silent"], IMAGE)

    assert [problem.split(":")[0] for problem in problems] == ["cached", "silent"]


class Cluster:
    """Two schedulable nodes and a control plane, a code-location Deployment, and a kubelet that caches images."""

    def __init__(self, *, secret_on_deployment: bool = True, purge_works: bool = True) -> None:
        self.cache: dict[str, set[str]] = {"cp": set(), "w1": {IMAGE}, "w2": {IMAGE}}
        self.cordoned: set[str] = set()
        self.purge_works = purge_works
        self.pods: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.counter = 0
        self.secret_on_deployment = secret_on_deployment
        self._start_pod("code-location", "w1")

    def _start_pod(self, prefix: str, node: str | None = None, **labels: str) -> str:
        self.counter += 1
        name = f"{prefix}-{self.counter}"
        node = node or next(n for n in ("w1", "w2") if n not in self.cordoned)
        if IMAGE in self.cache[node]:
            self.events.append(_event(name, f'Container image "{IMAGE}" already present on machine'))
        else:
            self.cache[node].add(IMAGE)
            self.events.append(_event(name, f'Successfully pulled image "{IMAGE}" in 1s'))
        spec = {"nodeName": node, "containers": [{"image": IMAGE}], "imagePullSecrets": [{"name": SECRET}]}
        self.pods[name] = {"kind": "Pod", "metadata": {"name": name, "labels": labels}, "spec": spec}
        return name

    def docker(self, args: list[str]) -> str:
        node = args[1]
        if args[3] == "images":
            images = [{"id": "sha256:1", "repoDigests": [IMAGE]}] if IMAGE in self.cache[node] else []
            return json.dumps({"images": images})
        if self.purge_works:
            self.cache[node].discard(IMAGE)
        return ""

    def kubectl(self, cfg: object, args: list[str], *, capture: bool = False, **_: object) -> str:
        verb = args[0]
        if verb == "get" and args[1] == "nodes":
            taint = [{"effect": "NoSchedule"}]
            items = [{"metadata": {"name": "cp"}, "spec": {"taints": taint}}]
            items += [
                {"metadata": {"name": node}, "spec": {"unschedulable": node in self.cordoned}} for node in ("w1", "w2")
            ]
            return json.dumps({"items": items})
        if verb == "get" and args[1] == "serviceaccount":
            return SECRET
        if verb == "get" and args[1] == "events":
            return json.dumps({"items": self.events})
        if verb == "get" and args[1].startswith("deployments"):
            deployment = _deployment("code-location", secrets=(SECRET,) if self.secret_on_deployment else ())
            return json.dumps({"items": [deployment, *self.pods.values()]})
        if verb == "get" and args[1] == "pods":
            selector = args[args.index("-l") + 1]
            wanted = selector.split("=", 1)[1]
            return json.dumps({"items": [p for p in self.pods.values() if wanted in p["metadata"]["labels"].values()]})
        if verb == "delete":
            for name in [a for a in args[2:] if a in self.pods]:
                del self.pods[name]
                self._start_pod("code-location", app="dagster-user-deployments")
        elif verb == "cordon":
            self.cordoned.add(args[1])
        elif verb == "uncordon":
            self.cordoned.discard(args[1])
        return ""


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> Cluster:
    state = Cluster()
    state.pods.clear()
    state._start_pod("code-location", "w1", app="dagster-user-deployments")
    state.events.clear()
    state.cache["w1"].add(IMAGE)

    @contextmanager
    def docker(_cfg: object) -> Iterator[Any]:
        yield state.docker

    @contextmanager
    def dagster(_cfg: object) -> Iterator[None]:
        yield None

    def launch(_cfg: object, _client: object, job: str) -> str:
        state._start_pod("run", run="run-1")
        return "run-1"

    monkeypatch.setattr(registry_pull, "kubectl", state.kubectl)
    monkeypatch.setattr(registry_pull, "_without_host_credentials", docker)
    monkeypatch.setattr(registry_pull, "dagster_client", dagster)
    monkeypatch.setattr(registry_pull, "launch_and_poll", launch)
    monkeypatch.setattr(registry_pull, "_active_image", lambda cfg, stage: IMAGE)
    monkeypatch.setattr(registry_pull, "_pull_secret_name", lambda cfg: SECRET)
    monkeypatch.setattr(registry_pull, "anonymous_manifest_status", lambda image: 403)
    monkeypatch.setattr(registry_pull.time, "sleep", lambda _s: None)
    # The fake cluster's selectors are label values, so the run pod is found by its run-id label.
    monkeypatch.setattr(registry_pull, "CODE_LOCATION_SELECTOR", "app=dagster-user-deployments")
    return state


def _cfg() -> Any:
    products = [SimpleNamespace(job_name="job")]
    return SimpleNamespace(
        env="local",
        namespace="olf-dev",
        environ={CONTRACT_STAGE_ENV: "dev"},
        inventory=SimpleNamespace(products=products),
    )


def test_pods_pull_through_the_secret_once_the_node_cache_is_gone(cluster: Cluster) -> None:
    registry_pull.check(_cfg())

    assert not cluster.cordoned
    assert cluster.cache["cp"] == set()


def test_a_cache_that_cannot_be_removed_is_reported_as_a_pull_that_never_happened(cluster: Cluster) -> None:
    cluster.purge_works = False

    with pytest.raises(E2EError, match="already present on the node"):
        registry_pull.check(_cfg())


def test_a_deployment_without_the_secret_fails_before_anything_is_removed(cluster: Cluster) -> None:
    cluster.secret_on_deployment = False

    with pytest.raises(E2EError, match=r"Deployment/code-location"):
        registry_pull.check(_cfg())

    assert IMAGE in cluster.cache["w1"]


def test_a_public_image_fails_unless_it_is_allowed(cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_pull, "anonymous_manifest_status", lambda image: 200)

    with pytest.raises(E2EError, match="image is public: pull-secret path not exercised"):
        registry_pull.check(_cfg())
    registry_pull.check(_cfg(), allow_public=True)


def test_static_only_touches_no_node(cluster: Cluster) -> None:
    registry_pull.check(_cfg(), static_only=True)

    assert IMAGE in cluster.cache["w1"] and IMAGE in cluster.cache["w2"]
