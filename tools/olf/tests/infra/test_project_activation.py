from __future__ import annotations

import json
from pathlib import Path

import pytest

from olf.common.project import ProjectSpec
from olf.infra import project_activation, project_revision
from olf.infra.artifact_store import FilesystemRevisionStore

_IMAGE = "ghcr.io/openlakeforge/project-code@sha256:" + "a" * 64
_DIGEST = "sha256:" + "b" * 64


def _activation() -> project_activation.ProjectActivation:
    return project_activation.ProjectActivation(
        deployment_profile="example",
        provider="local",
        stage="dev",
        project_name="widgets",
        project_revision=_DIGEST,
        distribution_version="0.3.0-alpha.1",
        project_code_image=_IMAGE,
        provider_binding_digest="sha256:" + "d" * 64,
        capabilities={"analytics": True, "governance": False},
    )


# Published by olf before Floe 0.8, when every activation bound the Floe
# manifests its stage rendered; the digest is what that code computed.
_SCHEMA_1_FLOE = "sha256:" + "c" * 64
_SCHEMA_1_REVISION = "sha256:e15344f2cfb4f6753d45d2a1793ba2f540d6c6836cbd0e91ba30c233540d1ceb"


def test_activation_hash_is_deterministic_and_round_trips() -> None:
    first = _activation().resolved()
    second = _activation().resolved()

    assert first.activation_revision == second.activation_revision
    assert project_activation.ProjectActivation.from_json(first.to_json()) == first


def test_a_new_activation_is_schema_2_without_a_floe_revision() -> None:
    payload = json.loads(_activation().to_json())

    assert payload["schema_version"] == 2
    assert "floe_manifest_revision" not in payload


def test_a_schema_1_record_still_reads_and_verifies_as_published() -> None:
    """A stage activated before Floe 0.8 must stay inspectable and a valid rollback target."""
    from dataclasses import replace

    legacy = replace(_activation(), floe_manifest_revision=_SCHEMA_1_FLOE).resolved()
    payload = json.loads(legacy.to_json())

    assert payload["schema_version"] == 1
    assert legacy.activation_revision == _SCHEMA_1_REVISION
    assert project_activation.ProjectActivation.from_json(legacy.to_json()) == legacy


def test_matches_inputs_ignores_a_schema_1_floe_revision() -> None:
    from dataclasses import replace

    activation = _activation().resolved()
    legacy = replace(activation, floe_manifest_revision=_SCHEMA_1_FLOE, activation_revision="").resolved()

    assert activation.matches_inputs(legacy)
    assert activation != legacy


def test_a_schema_2_record_carrying_a_floe_revision_is_rejected() -> None:
    from dataclasses import replace

    payload = json.loads(replace(_activation(), floe_manifest_revision=_SCHEMA_1_FLOE).to_json())
    payload["schema_version"] = 2

    with pytest.raises(project_activation.ProjectActivationError, match="unsupported field set"):
        project_activation.ProjectActivation.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capabilities", {"analytics": False, "governance": False}),
        ("provider_binding_digest", "sha256:" + "f" * 64),
        ("project_revision", "sha256:" + "1" * 64),
        ("project_code_image", "ghcr.io/openlakeforge/project-code@sha256:" + "2" * 64),
        ("stage", "prod"),
    ],
)
def test_matches_inputs_rejects_a_changed_activation_input(field: str, value: object) -> None:
    from dataclasses import replace

    activation = _activation().resolved()
    changed = replace(activation, **{field: value}, activation_revision="").resolved()

    assert not activation.matches_inputs(changed)


def test_activation_rejects_unknown_fields() -> None:
    payload = json.loads(_activation().to_json())
    payload["unexpected"] = True

    with pytest.raises(project_activation.ProjectActivationError, match="field set"):
        project_activation.ProjectActivation.from_json(json.dumps(payload))


def test_active_pointer_reads_immutable_activation(tmp_path: Path) -> None:
    store = FilesystemRevisionStore(tmp_path / "store")
    activation = project_activation.publish(store, _activation())
    project_activation.commit_active(store, activation)

    assert project_activation.active(store, stage="dev") == activation
    assert project_activation.active(store, stage="prod") is None


def test_materialize_uses_only_verified_published_content(external_project: Path, tmp_path: Path) -> None:
    distribution_root = Path(__file__).resolve().parents[4]
    spec = ProjectSpec(root=external_project, distribution_root=distribution_root)
    manifest = project_revision.build_project_revision(
        spec,
        image=_IMAGE,
        distribution_version="0.2.0-alpha.1",
    )
    store = FilesystemRevisionStore(tmp_path / "store")
    project_revision.publish(store, manifest, spec)

    source = external_project / "lakehouse_code" / "governance.yml"
    source.write_text("mutated after publish\n")
    restored = project_revision.materialize(store, manifest, tmp_path / "materialized")

    assert (restored / "lakehouse_code" / "governance.yml").read_text() != source.read_text()


def test_materialize_rejects_path_escape(tmp_path: Path) -> None:
    store = FilesystemRevisionStore(tmp_path / "store")
    component = project_revision.ComponentEntries("descriptors", {"../escape": "0" * 64})
    image = project_revision.ComponentEntries("image", {"project-code": _IMAGE})
    distribution = project_revision.ComponentEntries("distribution", {"version": "0.2.0-alpha.1"})
    required = [
        component,
        project_revision.ComponentEntries("floe", {}),
        project_revision.ComponentEntries("dbt", {}),
        project_revision.ComponentEntries("dagster", {}),
        image,
        distribution,
    ]
    manifest = project_revision.ProjectRevisionManifest(
        project_name="widgets",
        distribution_version="0.2.0-alpha.1",
        project_code_image=_IMAGE,
        components=tuple(required),
        revision=project_revision._aggregate_components(required),  # noqa: SLF001 - crafted hostile sidecar shape
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="unsafe artifact path"):
        project_revision.materialize(store, manifest, tmp_path / "materialized")


def _packaged_dagster_chart(root: Path) -> Path:
    """A parent chart shaped like `prepare_chart` output: subchart unpacked, not re-archived."""
    import tarfile

    tree = root / "tree"
    subchart = tree / "dagster" / "charts" / "dagster-user-deployments"
    (subchart / "templates").mkdir(parents=True)
    (tree / "dagster" / "Chart.yaml").write_text("name: dagster\nversion: 1.13.6\n")
    (subchart / "Chart.yaml").write_text("name: dagster-user-deployments\nversion: 1.13.6\n")
    (subchart / "values.yaml").write_text("deployments: []\n")
    (subchart / "templates" / "service-user.yaml").write_text("kind: Service\n")

    archive = root / "dagster-1.13.6.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(tree / "dagster", arcname="dagster")
    return archive


def test_user_chart_extracts_the_unpacked_subchart(tmp_path: Path) -> None:
    from olf.infra.deployment.activation import _user_chart

    chart = _user_chart(_packaged_dagster_chart(tmp_path), tmp_path / "work")

    assert (chart / "Chart.yaml").read_text().startswith("name: dagster-user-deployments")
    assert (chart / "templates" / "service-user.yaml").is_file()
    assert not (chart / "charts").exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("deployment_profile", "", "are required"),
        ("project_name", "", "are required"),
        ("distribution_version", "", "are required"),
        ("provider", "gcp", "unsupported activation provider"),
        ("stage", "staging", "unsupported activation stage"),
        ("project_revision", "sha256:short", "project_revision must be"),
        ("floe_manifest_revision", "not-a-digest", "floe_manifest_revision must be"),
        ("provider_binding_digest", "sha256:" + "G" * 64, "provider_binding_digest must be"),
        ("project_code_image", "ghcr.io/openlakeforge/project-code:latest", "must be digest-pinned"),
        ("capabilities", {"analytics": True, "lineage": True}, "may only declare analytics and governance"),
        ("capabilities", {"analytics": "yes"}, "outcomes must be booleans"),
    ],
)
def test_validate_rejects_an_unusable_activation(field: str, value: object, message: str) -> None:
    """An activation is the record a stage is later redeployed from, so a
    malformed one has to fail here rather than at the Helm rollout it reaches."""
    from dataclasses import replace

    activation = replace(_activation(), **{field: value})

    with pytest.raises(project_activation.ProjectActivationError, match=message):
        activation.validate(allow_unresolved=True)


def test_validate_requires_the_revision_once_the_activation_is_resolved() -> None:
    """`activation_revision` is empty until `resolved()` computes it, so the
    pre-hash pass tolerates exactly that one absence and the final pass does not."""
    activation = _activation()

    activation.validate(allow_unresolved=True)

    with pytest.raises(project_activation.ProjectActivationError, match="activation_revision must be"):
        activation.validate()
