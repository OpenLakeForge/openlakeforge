"""Immutable, stage-bound activation records for ProjectRevisions (#115).

The record, its keys and the readers live in `openlakeforge_core` so the alert evaluator can read them without
`olf`; this module keeps what writes them and turns a store failure into `ProjectActivationError`.
"""

from __future__ import annotations

from openlakeforge_core.project_activation import (
    ACTIVATION_API_VERSION,
    ACTIVATION_KIND,
    POINTER_KIND,
    ProjectActivation,
    ProjectActivationError,
    activation_key,
    active_key,
    pointer_document,
    read_activation,
    read_active,
)
from openlakeforge_core.stages import StageName

from olf.infra.artifact_store import ArtifactStoreError, RevisionStore, publish_immutable

__all__ = [
    "ACTIVATION_API_VERSION",
    "ACTIVATION_KIND",
    "POINTER_KIND",
    "ProjectActivation",
    "ProjectActivationError",
    "activation_key",
    "active",
    "active_key",
    "commit_active",
    "publish",
    "read",
]


def publish(store: RevisionStore, activation: ProjectActivation) -> ProjectActivation:
    resolved = activation.resolved()
    try:
        publish_immutable(
            store,
            activation_key(resolved.activation_revision, stage=resolved.stage),
            resolved.to_json().encode(),
            content_type="application/json",
        )
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc
    return resolved


def read(store: RevisionStore, activation_revision: str, *, stage: StageName | str) -> ProjectActivation:
    try:
        return read_activation(store, activation_revision, stage=stage)
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc


def commit_active(store: RevisionStore, activation: ProjectActivation) -> None:
    resolved = activation.resolved()
    try:
        store.write(
            active_key(resolved.stage), pointer_document(resolved.activation_revision), content_type="application/json"
        )
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc


def active(store: RevisionStore, *, stage: StageName | str) -> ProjectActivation | None:
    try:
        return read_active(store, stage=stage)
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc
