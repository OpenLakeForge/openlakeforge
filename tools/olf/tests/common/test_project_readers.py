"""The readers of `ACTIVE.json` and the revision sidecar that the alert evaluator uses without `olf` (ADR 0023)."""

from __future__ import annotations

import json

import pytest
from openlakeforge_core.project_activation import (
    ProjectActivation,
    ProjectActivationError,
    activation_key,
    active_key,
    pointer_document,
    read_activation,
    read_active,
)
from openlakeforge_core.project_revision import (
    ComponentEntries,
    ProjectRevisionError,
    aggregate_components,
    revision_prefix,
    sidecar_components,
    sidecar_key,
)


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def read(self, key: str) -> bytes | None:
        return self.objects.get(key)


def _activation(stage: str = "dev") -> ProjectActivation:
    return ProjectActivation(
        deployment_profile="example",
        provider="local",
        stage=stage,  # type: ignore[arg-type]
        project_name="widgets",
        project_revision="sha256:" + "b" * 64,
        distribution_version="0.3.0",
        project_code_image="ghcr.io/openlakeforge/project-code@sha256:" + "a" * 64,
        provider_binding_digest="sha256:" + "d" * 64,
        capabilities={"analytics": True},
    ).resolved()


def _activated(store: _Store, stage: str = "dev") -> ProjectActivation:
    activation = _activation(stage)
    store.objects[activation_key(activation.activation_revision, stage=stage)] = activation.to_json().encode()
    store.objects[active_key(stage)] = pointer_document(activation.activation_revision)
    return activation


def test_active_follows_the_pointer_to_the_record() -> None:
    store = _Store()
    activation = _activated(store)

    assert read_active(store, stage="dev") == activation


def test_a_stage_never_activated_has_no_active_record() -> None:
    assert read_active(_Store(), stage="prod") is None


def test_a_pointer_to_a_missing_record_is_an_error() -> None:
    store = _Store()
    activation = _activated(store)
    del store.objects[activation_key(activation.activation_revision, stage="dev")]

    with pytest.raises(ProjectActivationError, match="missing immutable artifact"):
        read_active(store, stage="dev")


def test_a_record_read_under_another_stage_is_refused() -> None:
    store = _Store()
    activation = _activated(store, "dev")
    # The same bytes filed where a prod reader looks: the record names dev, so prod must not accept it.
    store.objects[activation_key(activation.activation_revision, stage="prod")] = store.objects[
        activation_key(activation.activation_revision, stage="dev")
    ]

    with pytest.raises(ProjectActivationError, match="does not match its requested stage"):
        read_activation(store, activation.activation_revision, stage="prod")


@pytest.mark.parametrize("pointer", [b"not json", b"[]", json.dumps({"kind": "Other"}).encode()])
def test_a_malformed_pointer_is_an_error(pointer: bytes) -> None:
    store = _Store()
    store.objects[active_key("dev")] = pointer

    with pytest.raises(ProjectActivationError):
        read_active(store, stage="dev")


def _sidecar(components: list[ComponentEntries], *, revision: str | None = None, schema_version: int = 2) -> str:
    return json.dumps(
        {
            "apiVersion": "openlakeforge.io/v1alpha1",
            "kind": "ProjectRevision",
            "schema_version": schema_version,
            "components": [component.to_dict() for component in components],
            "revision": revision or aggregate_components(components),
        }
    )


def test_sidecar_components_are_tied_to_the_revision() -> None:
    components = [ComponentEntries("descriptors", {"lakehouse_code/alerts.yml": "c" * 64})]
    revision = aggregate_components(components)

    assert sidecar_components(_sidecar(components), revision) == tuple(components)
    assert sidecar_key(revision) == f"{revision_prefix(revision)}/PROJECT-REVISION.json"


def test_a_sidecar_edited_after_publication_is_refused() -> None:
    components = [ComponentEntries("descriptors", {"lakehouse_code/alerts.yml": "c" * 64})]
    revision = aggregate_components(components)
    edited = [ComponentEntries("descriptors", {"lakehouse_code/alerts.yml": "e" * 64})]

    with pytest.raises(ProjectRevisionError, match="aggregate"):
        sidecar_components(_sidecar(edited, revision=revision), revision)


def test_a_sidecar_filed_under_another_revision_is_refused() -> None:
    components = [ComponentEntries("descriptors", {"a": "c" * 64})]
    other = "sha256:" + "f" * 64

    with pytest.raises(ProjectRevisionError, match="not requested"):
        sidecar_components(_sidecar(components), other)


def test_a_sidecar_of_a_newer_schema_is_refused() -> None:
    components = [ComponentEntries("descriptors", {"a": "c" * 64})]

    with pytest.raises(ProjectRevisionError, match="unsupported PROJECT-REVISION.json schema_version"):
        sidecar_components(_sidecar(components, schema_version=99), aggregate_components(components))


def test_a_sidecar_with_a_second_component_of_the_same_name_is_refused() -> None:
    """The aggregate keeps the last digest per path, so the first `descriptors` could carry a forged digest."""
    genuine = ComponentEntries("descriptors", {"lakehouse_code/alerts.yml": "c" * 64})
    forged = ComponentEntries("descriptors", {"lakehouse_code/alerts.yml": "e" * 64})
    revision = aggregate_components([forged, genuine])

    with pytest.raises(ProjectRevisionError, match="duplicate component name"):
        sidecar_components(_sidecar([forged, genuine], revision=revision), revision)
