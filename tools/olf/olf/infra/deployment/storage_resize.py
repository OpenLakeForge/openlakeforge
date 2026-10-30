"""Growing the SeaweedFS volumes when `spec.storage.size` changes.

A PVC can only grow, and only where its StorageClass allows expansion. The
profile is compared with the PVCs that exist, so the answer holds whatever size
was applied before. `olf` never deletes or recreates a volume to apply a size.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from olf.common import log
from olf.common.deployment.errors import DeploymentPreconditionError
from olf.common.profile import Provider
from olf.common.storage_sizes import ObjectStoreVolumes, load_object_store_volumes, quantity_bytes
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.engine import DeploymentConfig

MIGRATION_DOC = "docs/reference/deployment-profile.md#changing-the-storage-size"

# The chart's StatefulSets are named `<release>-<role>` and their claim templates `<template>-<statefulset>-<n>`;
# the claims carry no labels to select on, and the master's template is named after the namespace.
_RELEASE = "seaweedfs"
_PVC_NAME = re.compile(rf"^(?P<template>.+)-(?P<statefulset>{_RELEASE}-(?P<role>master|filer|volume))-\d+$")
_DEFAULT_CLASS = "storageclass.kubernetes.io/is-default-class"


@dataclass(frozen=True)
class VolumeGrowth:
    pvc: str
    statefulset: str
    current: str
    target: str


def plan_storage_resize(
    config: DeploymentConfig, tools: Toolkit, *, kube_context: str, env: Mapping[str, str]
) -> tuple[VolumeGrowth, ...]:
    """The volumes that must grow to reach `spec.storage.size`, or a refusal naming why none can.

    Read-only. Raises before any mutation when a volume would shrink or its StorageClass cannot expand.
    """
    context = config.context
    if context.provider == Provider.AWS:  # object storage is managed S3: there is no volume
        return ()
    claims = _seaweedfs_claims(config, tools, kube_context=kube_context, env=env)
    if not claims:  # first install: nothing to grow or shrink
        return ()
    targets = load_object_store_volumes(config.paths.distribution_root)[context.topology.storage_size]

    growth: list[VolumeGrowth] = []
    refusals: list[str] = []
    for claim in claims:
        target = _target_for(targets, claim.role)
        if quantity_bytes(target) < quantity_bytes(claim.current):
            refusals.append(
                f"{claim.name} holds {claim.current} and storage.size '{context.topology.storage_size.value}' "
                f"asks for {target}: a volume never shrinks"
            )
        elif quantity_bytes(target) > quantity_bytes(claim.current):
            growth.append(VolumeGrowth(claim.name, claim.statefulset, claim.current, target))
    if growth:
        classes = _storage_classes(config, tools, kube_context=kube_context, env=env)
        class_of = {claim.name: claim.storage_class or classes.default for claim in claims}
        for step in growth:
            storage_class = class_of[step.pvc]
            if not classes.expandable.get(storage_class, False):
                refusals.append(
                    f"{step.pvc} would grow {step.current} to {step.target}, but StorageClass '{storage_class}' "
                    "does not set allowVolumeExpansion: true"
                )
    if refusals:
        raise DeploymentPreconditionError(
            "refusing to change spec.storage.size before anything is applied:\n  - "
            + "\n  - ".join(refusals)
            + "\nolf never deletes and recreates a volume to apply a size. Keep the size the volumes already have, "
            f"or migrate to a new deployment at the size you want ({MIGRATION_DOC})."
        )
    return tuple(growth)


def grow_seaweedfs_volumes(
    config: DeploymentConfig, tools: Toolkit, *, kube_context: str, env: Mapping[str, str]
) -> None:
    """Expand the claims `plan_storage_resize` names, then release the StatefulSets whose templates are stale.

    Kubernetes forbids editing a StatefulSet's volume claim templates, which is where the chart writes the size,
    so the Helm upgrade that follows would fail. Deleting a StatefulSet with `--cascade=orphan` removes only the
    controller object: pods and volumes keep running, and the upgrade recreates it with the new template. Which
    StatefulSets to release is read from their own templates, not from the claims grown in this run, so a retry
    after a run that stopped between the patch and the delete still releases them.
    """
    growth = plan_storage_resize(config, tools, kube_context=kube_context, env=env)
    if config.context.provider == Provider.AWS:
        return
    namespace = config.context.shared_namespace
    kubeconfig = config.paths.kubeconfig_path
    for step in growth:
        log.step(f"Expanding volume {namespace}/{step.pvc} from {step.current} to {step.target}...")
        tools.kubectl.patch(
            "pvc",
            step.pvc,
            json.dumps({"spec": {"resources": {"requests": {"storage": step.target}}}}),
            namespace=namespace,
            context=kube_context,
            kubeconfig=kubeconfig,
            env=env,
        )
    for statefulset in _stale_statefulsets(config, tools, kube_context=kube_context, env=env):
        tools.kubectl.delete(
            "statefulset",
            statefulset,
            namespace=namespace,
            context=kube_context,
            kubeconfig=kubeconfig,
            extra_args=("--cascade=orphan",),
            env=env,
        )


@dataclass(frozen=True)
class _Classes:
    expandable: Mapping[str, bool]
    default: str


@dataclass(frozen=True)
class _Claim:
    name: str
    role: str
    statefulset: str
    current: str
    storage_class: str


def _target_for(targets: ObjectStoreVolumes, role: str) -> str:
    return {"master": targets.master, "filer": targets.filer, "volume": targets.volume}[role]


def _items(document: str, *, what: str) -> list[dict[str, Any]]:
    if not document.strip():
        return []
    try:
        items = json.loads(document)["items"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DeploymentPreconditionError(f"kubectl returned {what} that is not a list: {document[:200]!r}") from exc
    return [item for item in items if isinstance(item, dict)]


def _seaweedfs_claims(
    config: DeploymentConfig, tools: Toolkit, *, kube_context: str, env: Mapping[str, str]
) -> list[_Claim]:
    namespace = config.context.shared_namespace
    result = tools.kubectl.get(
        "pvc",
        namespace=namespace,
        context=kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        output="json",
        env=env,
        check=False,
    )
    if not result.ok:
        raise DeploymentPreconditionError(
            f"could not list the PersistentVolumeClaims in namespace '{namespace}': "
            f"{(result.stderr or result.stdout).strip() or 'non-zero status'}. Whether spec.storage.size would "
            "shrink or grow a volume cannot be determined; restore access to the cluster and re-run."
        )
    claims: list[_Claim] = []
    for item in _items(result.stdout, what="PersistentVolumeClaims"):
        name = item["metadata"]["name"]
        match = _PVC_NAME.match(name)
        if match is None:
            continue
        spec = item.get("spec", {})
        claims.append(
            _Claim(
                name,
                match["role"],
                match["statefulset"],
                spec["resources"]["requests"]["storage"],
                spec.get("storageClassName") or "",
            )
        )
    return claims


def _stale_statefulsets(
    config: DeploymentConfig, tools: Toolkit, *, kube_context: str, env: Mapping[str, str]
) -> list[str]:
    """The SeaweedFS StatefulSets whose claim templates request a size other than `spec.storage.size`'s."""
    namespace = config.context.shared_namespace
    result = tools.kubectl.get(
        "statefulset",
        namespace=namespace,
        context=kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        output="json",
        env=env,
        check=False,
    )
    if not result.ok:
        raise DeploymentPreconditionError(
            f"could not list the StatefulSets in namespace '{namespace}': "
            f"{(result.stderr or result.stdout).strip() or 'non-zero status'}. Whether a SeaweedFS StatefulSet "
            "still carries the old volume size cannot be determined; restore access to the cluster and re-run."
        )
    roles = {f"{_RELEASE}-{role}": role for role in ("master", "filer", "volume")}
    seaweedfs = [item for item in _items(result.stdout, what="StatefulSets") if item["metadata"]["name"] in roles]
    if not seaweedfs:  # first install: the apply creates them at the profile's size
        return []
    targets = load_object_store_volumes(config.paths.distribution_root)[config.context.topology.storage_size]
    stale: list[str] = []
    for item in seaweedfs:
        name = item["metadata"]["name"]
        target = quantity_bytes(_target_for(targets, roles[name]))
        templates = item.get("spec", {}).get("volumeClaimTemplates", [])
        requests = [t.get("spec", {}).get("resources", {}).get("requests", {}).get("storage") for t in templates]
        if any(request and quantity_bytes(request) != target for request in requests):
            stale.append(name)
    return stale


def _storage_classes(
    config: DeploymentConfig, tools: Toolkit, *, kube_context: str, env: Mapping[str, str]
) -> _Classes:
    result = tools.kubectl.get(
        "storageclass",
        context=kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        output="json",
        env=env,
        check=False,
    )
    if not result.ok:
        raise DeploymentPreconditionError(
            "could not read the cluster's StorageClasses: "
            f"{(result.stderr or result.stdout).strip() or 'non-zero status'}. Whether a volume can grow cannot be "
            "determined; restore access to the cluster and re-run."
        )
    items = _items(result.stdout, what="StorageClasses")
    default = next(
        (
            item["metadata"]["name"]
            for item in items
            if item["metadata"].get("annotations", {}).get(_DEFAULT_CLASS) == "true"
        ),
        "",
    )
    return _Classes({item["metadata"]["name"]: item.get("allowVolumeExpansion") is True for item in items}, default)
