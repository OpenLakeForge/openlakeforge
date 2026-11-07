"""Registry pull check (#537): user-code pods name the stage's pull Secret, for an image that needs one.

A private customer image must stay pullable after the operator and the CI runner are gone, and on a node that has
never seen it. That only holds if every pod running the image lists the Secret in `imagePullSecrets`.

Every workload and pod that runs the active project image must list it, and so must the `dagster` ServiceAccount
(Floe Jobs are created at run time and can only inherit it from there). The check is meaningless for a public image,
which pulls without the Secret, so it refuses one unless told otherwise. It never reads a Secret's data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import requests

from olf.common import config, log
from olf.common.profile import resolve_declared_project
from olf.infra import project_activation
from olf.infra.artifact_store import ArtifactStoreError, S3RevisionStore, artifact_bucket, artifact_storage_client
from olf.infra.contracts import CONTRACT_STAGE_ENV
from olf.infra.e2e._shell import E2EConfig, E2EError, cfg_environ, kubectl

# Everything that can carry a pod spec; Pods themselves are listed too, for the ones a Job made at run time.
WORKLOAD_KINDS = ("deployments", "statefulsets", "cronjobs", "jobs", "pods")
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


def missing_pull_secret(items: Sequence[Mapping[str, Any]], image: str, secret: str) -> tuple[list[str], int]:
    """`Kind/name` of each workload running `image` without `secret` in its imagePullSecrets, and how many run it."""
    missing: list[str] = []
    users = 0
    for item in items:
        spec = _pod_spec(item)
        containers = [*(spec.get("containers") or []), *(spec.get("initContainers") or [])]
        if not any(container.get("image") == image for container in containers):
            continue
        users += 1
        if secret not in [entry.get("name") for entry in spec.get("imagePullSecrets") or []]:
            missing.append(f"{item.get('kind')}/{item.get('metadata', {}).get('name')}")
    return missing, users


def _workloads(cfg: E2EConfig) -> list[dict[str, Any]]:
    payload = json.loads(
        kubectl(cfg, ["get", ",".join(WORKLOAD_KINDS), "-n", cfg.namespace, "-o", "json"], capture=True)
    )
    return list(payload.get("items", []))


def check_static(cfg: E2EConfig, image: str, secret: str) -> int:
    """Fail unless every workload running `image` and the `dagster` ServiceAccount list `secret`; returns the count."""
    missing, users = missing_pull_secret(_workloads(cfg), image, secret)
    account = kubectl(
        cfg,
        ["get", "serviceaccount", "dagster", "-n", cfg.namespace, "-o", "jsonpath={.imagePullSecrets[*].name}"],
        capture=True,
    ).split()
    if secret not in account:
        missing.append("ServiceAccount/dagster (inherited by Floe Jobs)")
    if users == 0:
        raise E2EError(f"no workload in {cfg.namespace} runs {image}; nothing to check.")
    if missing:
        raise E2EError(f"{secret!r} is missing from imagePullSecrets of: {', '.join(missing)}.")
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


def check(cfg: E2EConfig, *, allow_public: bool = False) -> None:
    stage = config.env(CONTRACT_STAGE_ENV, "dev", environ=cfg.environ)
    secret = _pull_secret_name(cfg)
    image = _active_image(cfg, stage)
    log.step(f"Stage {stage}: checking that {image} is pulled through Secret {secret!r}...")
    users = check_static(cfg, image, secret)
    log.info(f"{users} workload(s) running the image list {secret!r}, and so does the dagster ServiceAccount.")
    require_private(image, allow_public=allow_public)
