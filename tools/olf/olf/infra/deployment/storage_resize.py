"""Checking `spec.storage.size` against the SeaweedFS volumes that exist.

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


def require_storage_size_applies(
    config: DeploymentConfig, tools: Toolkit, *, kube_context: str, env: Mapping[str, str]
) -> None:
    """Refuse, before any mutation, a `spec.storage.size` the existing volumes cannot take.

    `olf` does not grow volumes in place yet, so even an expandable StorageClass is refused for now: the size is
    chosen when a deployment is created. Growing in place is the follow-up to #326.
    """
    growth = plan_storage_resize(config, tools, kube_context=kube_context, env=env)
    if growth:
        raise DeploymentPreconditionError(
            "refusing to change spec.storage.size before anything is applied:\n  - "
            + "\n  - ".join(f"{step.pvc} would grow {step.current} to {step.target}" for step in growth)
            + "\nolf does not grow existing volumes in place yet (#326). Keep the size the volumes already have, "
            f"or migrate to a new deployment at the size you want ({MIGRATION_DOC})."
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
