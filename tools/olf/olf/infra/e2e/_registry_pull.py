"""Registry pull check (#537): user-code pods pull their image through the stage's pull Secret.

A private customer image must stay pullable after the operator and the CI runner are gone, and on a node that has
never seen it. On kind that is not true by default: activation pulls the image on the host and loads it into every
node (`load_digest_image_into_kind`), so a missing or wrong `imagePullSecrets` entry stays hidden until the cache is
gone. This check removes the cache and proves the kubelet pulls again, with the Secret.

Static: every Deployment, StatefulSet and CronJob that runs the active project image lists the Secret, and the
`dagster` ServiceAccount does too (Floe Jobs are created at run time and can only inherit it from there). Run Jobs and
Pods are judged only when created after the check starts: earlier ones are immutable history. Live: remove the image
from every node, restart the code locations, launch a run, and require a real pull event for each new pod. The check is
meaningless for a public image, so it refuses one unless told otherwise. It never reads a Secret's data.
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import requests

from olf.common import config, log
from olf.common.profile import resolve_declared_project
from olf.common.tooling.resolver import build_resolver
from olf.infra import project_activation
from olf.infra.artifact_store import ArtifactStoreError, S3RevisionStore, artifact_bucket, artifact_storage_client
from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e._dagster import (
    dagster_client,
    launch_and_poll,
)
from olf.infra.e2e._shell import E2EConfig, E2EError, _run, cfg_environ, kubectl

# What the activation renders and keeps: their pod templates change with every activation. Jobs and Pods are not
# here on purpose: a Job's template is immutable, so the Dagster run Jobs of an earlier suite keep the spec they were
# created with and say nothing about the current activation.
CONTROLLER_KINDS = ("deployments", "statefulsets", "cronjobs")
# Everything that can carry a pod spec; Jobs and Pods are the ones the run launcher and Floe make at run time.
WORKLOAD_KINDS = (*CONTROLLER_KINDS, "jobs", "pods")
CODE_LOCATION_SELECTOR = "app.kubernetes.io/name=dagster-user-deployments"
ROLLOUT_TIMEOUT = "300s"
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
ANONYMOUS_PROBE_TIMEOUT_SECONDS = 20


def _pod_spec(item: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = item.get("spec") or {}
    if item.get("kind") == "Pod":
        return spec
    if item.get("kind") == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def workloads_missing_entry(items: Sequence[Mapping[str, Any]], image: str, secret_name: str) -> tuple[list[str], int]:
    """`Kind/name` of each workload running `image` missing `secret_name` from imagePullSecrets, and how many run it."""
    missing: list[str] = []
    users = 0
    for item in items:
        spec = _pod_spec(item)
        containers = [*(spec.get("containers") or []), *(spec.get("initContainers") or [])]
        if not any(container.get("image") == image for container in containers):
            continue
        users += 1
        if secret_name not in [entry.get("name") for entry in spec.get("imagePullSecrets") or []]:
            missing.append(f"{item.get('kind')}/{item.get('metadata', {}).get('name')}")
    return missing, users


def _workloads(cfg: E2EConfig, kinds: Sequence[str] = WORKLOAD_KINDS) -> list[dict[str, Any]]:
    payload = json.loads(kubectl(cfg, ["get", ",".join(kinds), "-n", cfg.namespace, "-o", "json"], capture=True))
    return list(payload.get("items", []))


def _identity(item: Mapping[str, Any]) -> tuple[str, str]:
    return str(item.get("kind")), str(item.get("metadata", {}).get("name"))


def created_since(items: Sequence[Mapping[str, Any]], baseline: set[tuple[str, str]]) -> list[Mapping[str, Any]]:
    """Items that were not in `baseline`: workloads made after it was taken."""
    return [item for item in items if _identity(item) not in baseline]


def check_static(cfg: E2EConfig, image: str, secret_name: str) -> int:
    """Fail unless all workloads with `image` and the dagster ServiceAccount list `secret_name`; returns the count."""
    # Controllers only: Jobs and Pods left by runs from before this activation were created without the Secret
    # and cannot be edited; the run check below proves the ones created after it.
    missing, users = workloads_missing_entry(_workloads(cfg, CONTROLLER_KINDS), image, secret_name)
    account = kubectl(
        cfg,
        ["get", "serviceaccount", "dagster", "-n", cfg.namespace, "-o", "jsonpath={.imagePullSecrets[*].name}"],
        capture=True,
    ).split()
    if secret_name not in account:
        missing.append("ServiceAccount/dagster (inherited by Floe Jobs)")
    if users == 0:
        raise E2EError(f"no workload in {cfg.namespace} runs {image}; nothing to check.")
    if missing:
        raise E2EError(f"{secret_name!r} is missing from imagePullSecrets of: {', '.join(missing)}.")
    return users


def _challenge(header: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def anonymous_manifest_status(image: str, *, session: requests.Session | None = None) -> int:
    """HTTP status of an anonymous manifest request for `image`; 200 means anyone can pull it."""
    repository, digest = image.split("@", 1)
    head, _, rest = repository.partition("/")
    if "." in head or ":" in head or head == "localhost":
        host, name = head, rest
    else:  # Docker Hub
        host, name = "registry-1.docker.io", repository if "/" in repository else f"library/{repository}"
    http = session or requests.Session()
    url = f"https://{host}/v2/{name}/manifests/{digest}"
    headers = {"Accept": MANIFEST_ACCEPT}
    try:
        response = http.get(url, headers=headers, timeout=ANONYMOUS_PROBE_TIMEOUT_SECONDS)
        if response.status_code == 401:
            challenge = _challenge(response.headers.get("WWW-Authenticate", ""))
            if "realm" not in challenge:
                return response.status_code
            token = http.get(
                challenge["realm"],
                params={"service": challenge.get("service", ""), "scope": f"repository:{name}:pull"},
                timeout=ANONYMOUS_PROBE_TIMEOUT_SECONDS,
            )
            token_value = token.json().get("token") or token.json().get("access_token") if token.ok else None
            if not token_value:
                return token.status_code
            response = http.get(
                url,
                headers={**headers, "Authorization": f"Bearer {token_value}"},
                timeout=ANONYMOUS_PROBE_TIMEOUT_SECONDS,
            )
        return response.status_code
    except (requests.RequestException, ValueError) as exc:
        raise E2EError(f"could not tell whether {image} is public: {exc}") from exc


def require_private(image: str, *, allow_public: bool) -> None:
    status = anonymous_manifest_status(image)
    if status != 200:
        log.info(
            f"{image} is not anonymously pullable (HTTP {status}); the pull Secret is what lets the cluster pull it."
        )
        return
    message = f"image is public: pull-secret path not exercised ({image} answered an anonymous manifest request)."
    if not allow_public:
        raise E2EError(f"{message} Pass --allow-public to run the rest anyway.")
    log.warn(
        f"{message} Continuing under --allow-public; this run proves the mechanism, not that the image is private."
    )


def image_ids(listing: str, repository: str) -> list[str]:
    """IDs in `crictl images -o json` output that carry a tag or digest name under `repository`."""
    ids = []
    for image in json.loads(listing).get("images", []):
        names = [*(image.get("repoTags") or []), *(image.get("repoDigests") or [])]
        if any(name.startswith((f"{repository}:", f"{repository}@")) for name in names):
            ids.append(image["id"])
    return ids


def containerd_refs(listing: str) -> dict[str, str]:
    """Image name to target digest from `ctr images ls` (columns REF TYPE DIGEST SIZE ...); the header is skipped."""
    refs = {}
    for line in listing.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 3:
            refs[columns[0]] = columns[2]
    return refs


def stale_refs(refs: Mapping[str, str], digests: set[str]) -> list[str]:
    """Names in containerd that still point at one of `digests`, whatever they are called."""
    return sorted(name for name, digest in refs.items() if digest in digests)


def pod_diagnostics(pods: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> list[str]:
    """One line per container that is not running (waiting reason and message), then each pod's recent events."""
    lines = []
    for pod in pods:
        name = pod.get("metadata", {}).get("name")
        status = pod.get("status") or {}
        for container in [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]:
            waiting = (container.get("state") or {}).get("waiting")
            if waiting:
                lines.append(f"{name}/{container.get('name')}: {waiting.get('reason')}: {waiting.get('message', '')}")
        pod_events = [
            event
            for event in events
            if event.get("involvedObject", {}).get("kind") == "Pod"
            and event.get("involvedObject", {}).get("name") == name
        ]
        pod_events.sort(key=lambda event: str(event.get("lastTimestamp") or event.get("eventTime") or ""))
        lines.extend(f"{name} event {event.get('reason')}: {event.get('message')}" for event in pod_events[-8:])
    return lines


def pull_failures(events: Sequence[Mapping[str, Any]], pods: Sequence[str], image: str) -> list[str]:
    """Pods with no `Successfully pulled` event for `image`, or one that found it already on the node."""
    repository = image.split("@", 1)[0]
    problems = []
    for pod in pods:
        messages = [
            str(event.get("message", ""))
            for event in events
            if event.get("involvedObject", {}).get("kind") == "Pod"
            and event.get("involvedObject", {}).get("name") == pod
            and event.get("reason") in ("Pulled", "Pulling")
            and repository in str(event.get("message", ""))
        ]
        if any("already present on machine" in message for message in messages):
            problems.append(f"{pod}: reported the image already present on the node")
        elif not any("Successfully pulled" in message for message in messages):
            problems.append(f"{pod}: no 'Successfully pulled' event")
    return problems


def _active_image(cfg: E2EConfig, stage: str) -> str:
    bucket = artifact_bucket(environ=cfg.environ)
    try:
        with artifact_storage_client("port-forward", bucket, environ=cfg.environ) as client:
            activation = project_activation.active(S3RevisionStore(client, bucket), stage=stage)
    except (ArtifactStoreError, project_activation.ProjectActivationError) as exc:
        raise E2EError(str(exc)) from exc
    if activation is None:
        raise E2EError(f"stage {stage} has no active project revision (no ACTIVE.json); deploy one first.")
    return activation.project_code_image


def _pull_secret_name(cfg: E2EConfig) -> str:
    topology = cfg.deployment.topology
    name = resolve_declared_project(topology.project, topology.provider, cfg_environ(cfg)).pull_secret_name
    if not name:
        raise E2EError(
            "spec.project.image.pull_secret_ref is not set in the profile; there is no pull Secret to prove."
        )
    return name


@dataclass(frozen=True)
class Node:
    name: str
    schedulable: bool


def _nodes(cfg: E2EConfig) -> list[Node]:
    payload = json.loads(kubectl(cfg, ["get", "nodes", "-o", "json"], capture=True))
    nodes = []
    for item in payload.get("items", []):
        tainted = any(taint.get("effect") == "NoSchedule" for taint in item.get("spec", {}).get("taints") or [])
        nodes.append(Node(item["metadata"]["name"], not tainted and not item.get("spec", {}).get("unschedulable")))
    return nodes


@contextlib.contextmanager
def _without_host_credentials(cfg: E2EConfig) -> Iterator[Callable[[list[str]], str]]:
    """A `docker` runner whose `DOCKER_CONFIG` is an empty directory, so no host login can be what makes a pull work."""
    environ = dict(cfg_environ(cfg))
    docker = str(build_resolver(environ=environ).resolve("docker"))
    with tempfile.TemporaryDirectory(prefix="olf-empty-docker-config-") as empty:
        environ["DOCKER_CONFIG"] = empty
        yield lambda args: _run([docker, *args], capture=True, env=environ)


def purge_image(cfg: E2EConfig, image: str) -> None:
    """Remove `image` (every tag and digest name of it) from every node's containerd.

    `kind load` also leaves an `import-<date>@sha256:...` name on the same target. `crictl rmi` does not remove it, and
    kubelet's next pull then reuses the image ID but CreateContainer resolves the stale name, whose manifest is gone:
    `CreateContainerError ... image "docker.io/library/import-...@sha256:..." not found`. So every name on the same
    target as a name under the repository goes too.
    """
    repository = image.split("@", 1)[0]
    ctr = ["ctr", "--namespace=k8s.io", "images"]
    with _without_host_credentials(cfg) as docker:
        for node in _nodes(cfg):
            ids = image_ids(docker(["exec", node.name, "crictl", "images", "-o", "json"]), repository)
            before = containerd_refs(docker(["exec", node.name, *ctr, "ls"]))
            targets = {
                digest for name, digest in before.items() if name.startswith((f"{repository}:", f"{repository}@"))
            }
            if ids:
                docker(["exec", node.name, "crictl", "rmi", *ids])
            left = stale_refs(containerd_refs(docker(["exec", node.name, *ctr, "ls"])), targets)
            if left:
                docker(["exec", node.name, *ctr, "rm", "--sync", *left])
            log.info(f"{node.name}: removed {len(ids)} cached image(s) and {len(left)} leftover name(s).")


def _code_location_pods(cfg: E2EConfig) -> list[dict[str, Any]]:
    payload = json.loads(
        kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-l", CODE_LOCATION_SELECTOR, "-o", "json"], capture=True)
    )
    return [pod for pod in payload.get("items", []) if not pod["metadata"].get("deletionTimestamp")]


def _explain_pods(cfg: E2EConfig) -> str:
    """Why the code-location pods are not Ready, from their container statuses and events; never raises."""
    try:
        pods = _code_location_pods(cfg)
        events = json.loads(kubectl(cfg, ["get", "events", "-n", cfg.namespace, "-o", "json"], capture=True)).get(
            "items", []
        )
        lines = pod_diagnostics(pods, events)
    except (E2EError, ValueError, KeyError) as exc:
        return f"(could not collect pod diagnostics: {exc})"
    return "\n".join(lines) if lines else "(no waiting container and no event on the code-location pods)"


def _replace_pods(cfg: E2EConfig, doomed: Sequence[str]) -> list[dict[str, Any]]:
    """Delete `doomed`, wait for their replacements to be Ready and return them."""
    kubectl(cfg, ["delete", "pod", *doomed, "-n", cfg.namespace, "--wait=true"])
    deadline = time.monotonic() + 300
    while not (pods := _code_location_pods(cfg)) or {pod["metadata"]["name"] for pod in pods} & set(doomed):
        if time.monotonic() > deadline:
            raise E2EError(f"no replacement for {', '.join(doomed)} appeared.")
        time.sleep(2)
    try:
        kubectl(
            cfg,
            [
                "wait",
                "--for=condition=Ready",
                "pod",
                "-l",
                CODE_LOCATION_SELECTOR,
                "-n",
                cfg.namespace,
                f"--timeout={ROLLOUT_TIMEOUT}",
            ],
        )
    except E2EError as exc:
        raise E2EError(
            f"the code location did not become Ready after its image was removed: {exc}\n{_explain_pods(cfg)}"
        ) from exc
    return _code_location_pods(cfg)


def _require_pulled(cfg: E2EConfig, pods: Sequence[str], image: str, *, what: str) -> None:
    events = json.loads(kubectl(cfg, ["get", "events", "-n", cfg.namespace, "-o", "json"], capture=True)).get(
        "items", []
    )
    problems = pull_failures(events, pods, image)
    if problems:
        raise E2EError(f"{what} did not pull through the Secret:\n  " + "\n  ".join(problems))
    log.info(f"{what}: {len(pods)} pod(s) pulled {image.split('@', 1)[0]} again.")


def _names(pods: Sequence[Mapping[str, Any]]) -> list[str]:
    return [pod["metadata"]["name"] for pod in pods]


def _check_restart(cfg: E2EConfig, image: str) -> None:
    log.step("Removing the image from every node and restarting the code locations...")
    purge_image(cfg, image)
    replacements = _replace_pods(cfg, _names(_code_location_pods(cfg)))
    _require_pulled(cfg, _names(replacements), image, what="code locations")


def _check_run(cfg: E2EConfig, image: str) -> None:
    log.step("Removing the image from every node and launching a run...")
    if not cfg.inventory.products:
        raise E2EError("the project has no product to launch a run for.")
    purge_image(cfg, image)
    # Jobs and Pods from earlier runs (the DEV suite runs before the Secret exists) keep the spec they were created
    # with, so only what appears after this point says whether the launcher carries the Secret.
    before = {_identity(item) for item in _workloads(cfg)}
    with dagster_client(cfg) as client:
        run_id = launch_and_poll(cfg, client, cfg.inventory.products[0].job_name)
    payload = json.loads(
        kubectl(cfg, ["get", "pods", "-n", cfg.namespace, "-l", f"dagster/run-id={run_id}", "-o", "json"], capture=True)
    )
    run_pods = [
        pod for pod in payload.get("items", []) if any(c.get("image") == image for c in pod["spec"]["containers"])
    ]
    if not run_pods:
        raise E2EError(f"run {run_id} left no pod running {image} to inspect.")
    _require_pulled(cfg, _names(run_pods), image, what=f"run {run_id}")
    # Floe Jobs exist only while a run is in flight, so the pods this run left are the only live look at them.
    fresh = created_since(_workloads(cfg), before)
    missing, users = workloads_missing_entry(fresh, image, _pull_secret_name(cfg))
    if missing:
        raise E2EError(f"after the run, {', '.join(missing)} run {image} without the pull Secret.")
    log.info(f"{users} workload(s) created by the run list the pull Secret.")


def _check_fresh_node(cfg: E2EConfig, image: str) -> None:
    schedulable = [node for node in _nodes(cfg) if node.schedulable]
    if len(schedulable) < 2:
        log.warn(
            "Only one schedulable node: the fresh-node step is skipped. Removing the image from the node above "
            "already proves no node-local cache is relied on."
        )
        return
    log.step("Cordoning the node that runs the code location and rescheduling it onto another...")
    pods = _code_location_pods(cfg)
    cordoned = pods[0]["spec"]["nodeName"]
    purge_image(cfg, image)
    kubectl(cfg, ["cordon", cordoned])
    try:
        replacements = _replace_pods(cfg, _names([pod for pod in pods if pod["spec"]["nodeName"] == cordoned]))
    finally:
        kubectl(cfg, ["uncordon", cordoned])
    moved = [pod for pod in replacements if pod["spec"]["nodeName"] == cordoned]
    if moved:
        raise E2EError(f"{', '.join(_names(moved))} were scheduled back onto cordoned node {cordoned}.")
    _require_pulled(cfg, _names(replacements), image, what=f"code locations rescheduled off {cordoned}")


def check(cfg: E2EConfig, *, allow_public: bool = False, static_only: bool = False) -> None:
    stage = config.env(CONTRACT_STAGE_ENV, "dev", environ=cfg.environ)
    secret_name = _pull_secret_name(cfg)
    image = _active_image(cfg, stage)
    log.step(f"Stage {stage}: checking that {image} is pulled through its pull Secret...")
    users = check_static(cfg, image, secret_name)
    log.info(f"{users} workload(s) running the image list the pull Secret, and so does the dagster ServiceAccount.")
    require_private(image, allow_public=allow_public)
    if static_only:
        return
    if cfg.env != "local":
        raise E2EError(
            "the live part removes images from kind nodes and runs only on the local provider; use --static-only."
        )
    _check_restart(cfg, image)
    _check_run(cfg, image)
    _check_fresh_node(cfg, image)
    log.info(f"Stage {stage}: the image is pulled through its pull Secret, not from a node-local cache.")
