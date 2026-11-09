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


def test_workloads_created_since_the_baseline_exclude_earlier_ones() -> None:
    old = {"kind": "Job", "metadata": {"name": "dagster-run-old"}}
    new = {"kind": "Job", "metadata": {"name": "dagster-run-new"}}

    assert registry_pull.created_since([old, new], {("Job", "dagster-run-old")}) == [new]


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


def _pod(name: str, node: str) -> dict[str, Any]:
    return {"metadata": {"name": name}, "spec": {"nodeName": node}}


def test_each_node_must_show_a_real_pull_and_a_later_pod_there_may_use_the_cache() -> None:
    events = [
        _event("run-pod", f'Successfully pulled image "{IMAGE}" in 2s'),
        _event("floe-job", f'Container image "{IMAGE}" already present on machine'),
        _event("cached", f'Container image "{IMAGE}" already present on machine'),
        _event("silent", "Started container"),
    ]
    pods = [_pod("run-pod", "w1"), _pod("floe-job", "w1"), _pod("cached", "w2"), _pod("silent", "w3")]

    problems = registry_pull.pull_failures(events, pods, IMAGE)

    assert problems == [
        "cached (node w2): reported the image already present on the node",
        "silent (node w3): no 'Successfully pulled' event",
    ]


class Cluster:
    """Two schedulable nodes and a control plane, a code-location Deployment, and a kubelet that caches images."""

    def __init__(self, *, secret_on_deployment: bool = True, purge_works: bool = True) -> None:
        self.cache: dict[str, set[str]] = {"cp": set(), "w1": {IMAGE}, "w2": {IMAGE}}
        self.cordoned: set[str] = set()
        # Names `kind load` left in containerd on the image, which the CRI record keeps listing after `crictl rmi`.
        self.held: dict[str, list[str]] = {}
        self.unrelated = "import-2026-06-02@sha256:other"
        self.cri_cache_stale = False
        self.ghost: dict[str, list[str]] = {}
        self.restart_clears_ghost = True
        self.restarts: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.purge_works = purge_works
        self.pods: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.counter = 0
        self.history: list[dict[str, Any]] = []
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

    def _record_names(self, node: str) -> list[str]:
        names = [IMAGE] if IMAGE in self.cache[node] else []
        names += [f"docker.io/library/{name}" for name in self.held.get(node, [])]
        return names or self.ghost.get(node, [])

    def docker(self, args: list[str]) -> str:
        node = args[1]
        if args[2] == "systemctl":
            self.restarts.append(node)
            self.calls.append(("restart", node))
            if self.restart_clears_ghost:
                self.ghost.pop(node, None)
            return ""
        if args[2] == "ctr":
            return self.ctr(node, args[5:])
        if args[3] == "info":
            return ""
        if args[3] == "images":
            names = self._record_names(node)
            return json.dumps({"images": [{"id": "sha256:1", "repoDigests": names}] if names else []})
        self.calls.append(("rmi", node))
        if self.purge_works:
            self.cache[node].discard(IMAGE)
        return ""

    def ctr(self, node: str, args: list[str]) -> str:
        if args[0] == "rm":
            self.calls.append(("ctr-rm", node))
            removed = [name for name in self.held.get(node, []) if name in args]
            self.held[node] = [name for name in self.held.get(node, []) if name not in args]
            assert self.unrelated not in args, "an import name of another image must not be removed"
            if self.cri_cache_stale and removed:
                self.ghost[node] = [f"docker.io/library/{name}" for name in removed]
            return ""
        names = [f"{REPOSITORY}@sha256:reg"] if IMAGE in self.cache[node] else []
        names += [*self.held.get(node, []), self.unrelated]
        header = "REF TYPE DIGEST SIZE PLATFORMS LABELS"
        return "\n".join([header, *(f"{n} application/vnd.oci.image.index.v1+json sha256:x 360MiB - -" for n in names)])

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
            kinds = args[1].split(",")
            deployment = _deployment("code-location", secrets=(SECRET,) if self.secret_on_deployment else ())
            pods = self.pods.values() if "pods" in kinds else ()
            history = self.history if "jobs" in kinds else ()
            return json.dumps({"items": [deployment, *history, *pods]})
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


def test_a_cache_that_cannot_be_removed_fails_the_purge_before_any_pod_is_judged(cluster: Cluster) -> None:
    cluster.purge_works = False

    with pytest.raises(E2EError, match="CRI image record survived"):
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


def test_run_jobs_from_before_the_secret_was_configured_are_not_judged(cluster: Cluster) -> None:
    # The DEV suite ran before `pull_secret_ref` existed; its Jobs keep the spec they were created with.
    pod = {"containers": [{"image": IMAGE}]}
    cluster.history.append(
        {"kind": "Job", "metadata": {"name": "dagster-run-historic"}, "spec": {"template": {"spec": pod}}}
    )

    registry_pull.check(_cfg())


def test_a_run_job_created_after_activation_without_the_secret_fails(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    pod = {"containers": [{"image": IMAGE}]}

    def launch(_cfg: object, _client: object, job: str) -> str:
        cluster._start_pod("run", run="run-1")
        cluster.history.append(
            {"kind": "Job", "metadata": {"name": "dagster-run-fresh"}, "spec": {"template": {"spec": pod}}}
        )
        return "run-1"

    monkeypatch.setattr(registry_pull, "launch_and_poll", launch)

    with pytest.raises(E2EError, match=r"Job/dagster-run-fresh"):
        registry_pull.check(_cfg())


def test_containerd_names_are_matched_to_digests_by_column() -> None:
    listing = "REF TYPE DIGEST SIZE\na t sha256:1 1MiB\nb t sha256:2 1MiB\n"

    assert registry_pull.containerd_refs(listing) == {"a": "sha256:1", "b": "sha256:2"}


def test_a_pod_that_cannot_start_is_explained_by_its_waiting_reason_and_events() -> None:
    pod = {
        "metadata": {"name": "code-1"},
        "status": {
            "containerStatuses": [
                {"name": "dagster", "state": {"waiting": {"reason": "ErrImagePull", "message": "403 Forbidden"}}}
            ]
        },
    }
    events = [_event("code-1", "pull denied", reason="Failed"), _event("other", "noise")]

    lines = registry_pull.pod_diagnostics([pod], events)

    assert lines == ["code-1/dagster: ErrImagePull: 403 Forbidden", "code-1 event Failed: pull denied"]


def test_a_code_location_that_never_becomes_ready_names_the_cause(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cluster.kubectl

    def kubectl(cfg: object, args: list[str], **kwargs: object) -> str:
        if args[0] == "wait" and "pod" in args:
            for pod in cluster.pods:
                cluster.events.append(_event(pod, "image not found", reason="Failed"))
            raise E2EError("timed out waiting for the condition")
        return real(cfg, args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_pull, "kubectl", kubectl)

    with pytest.raises(E2EError, match="Failed: image not found"):
        registry_pull.check(_cfg())


def test_the_names_a_record_keeps_after_rmi_are_removed_with_ctr_and_nothing_else(cluster: Cluster) -> None:
    held = ["import-2026-11-09@sha256:37378c", "import-2026-11-09@sha256:ea8f63"]
    cluster.held = {"w1": list(held), "w2": list(held)}

    registry_pull.purge_image(_cfg(), IMAGE)

    assert cluster.held == {"w1": [], "w2": []}
    assert cluster.restarts == []
    assert [call[0] for call in cluster.calls if call[1] == "w1"] == ["rmi", "ctr-rm"]


def test_a_record_that_survives_rmi_ctr_and_a_restart_fails_the_purge_naming_node_id_and_names(
    cluster: Cluster,
) -> None:
    cluster.held = {"w1": ["import-2026-11-09@sha256:37378c"]}
    cluster.cri_cache_stale = True
    cluster.restart_clears_ghost = False

    with pytest.raises(E2EError, match=r"w1: the CRI image record survived.*sha256:1.*import-2026-11-09"):
        registry_pull.purge_image(_cfg(), IMAGE)

    assert [call[0] for call in cluster.calls if call[1] == "w1"] == ["rmi", "ctr-rm", "rmi", "restart"]


def test_containerd_is_restarted_only_when_a_record_still_lists_and_that_rebuilds_it(cluster: Cluster) -> None:
    cluster.held = {"w1": ["import-2026-11-09@sha256:37378c"]}
    cluster.cri_cache_stale = True

    registry_pull.purge_image(_cfg(), IMAGE)

    assert cluster.restarts == ["w1"]
    assert cluster.ghost == {}


def test_a_run_that_launches_several_pods_on_one_node_passes_when_the_first_pulled(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch(_cfg: object, _client: object, job: str) -> str:
        cluster._start_pod("run", "w1", run="run-1")
        cluster._start_pod("floe", "w1", run="run-1")
        return "run-1"

    monkeypatch.setattr(registry_pull, "launch_and_poll", launch)

    registry_pull.check(_cfg())


def test_a_run_whose_pods_all_found_a_cache_fails_even_with_several_pods(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch(_cfg: object, _client: object, job: str) -> str:
        cluster.cache["w1"].add(IMAGE)
        cluster._start_pod("run", "w1", run="run-1")
        cluster._start_pod("floe", "w1", run="run-1")
        return "run-1"

    monkeypatch.setattr(registry_pull, "launch_and_poll", launch)

    with pytest.raises(E2EError, match=r"node w1\): reported the image already present on the node"):
        registry_pull.check(_cfg())
