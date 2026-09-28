from __future__ import annotations

from pathlib import Path

import pytest

from olf import project_revision
from olf.artifact_store import FilesystemRevisionStore
from olf.project import ProjectSpec

_DISTRIBUTION_ROOT = Path(__file__).resolve().parents[3]
_DIGEST_IMAGE = "ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "a" * 64
_DISTRIBUTION_VERSION = "0.2.0-alpha.1"


def _spec(root: Path) -> ProjectSpec:
    return ProjectSpec(root=root, distribution_root=_DISTRIBUTION_ROOT)


def _build(root: Path) -> project_revision.ProjectRevisionManifest:
    return project_revision.build_project_revision(
        _spec(root), image=_DIGEST_IMAGE, distribution_version=_DISTRIBUTION_VERSION
    )


def test_revision_is_stable_across_repeated_builds(external_project: Path) -> None:
    first = _build(external_project)
    second = _build(external_project)

    assert first.revision == second.revision
    assert first.to_json() == second.to_json()


def test_revision_is_stable_across_a_copy_at_a_different_path(external_project: Path, tmp_path: Path) -> None:
    import shutil

    copy_root = tmp_path / "copy-of-project"
    shutil.copytree(external_project, copy_root)

    assert _build(external_project).revision == _build(copy_root).revision


def test_editing_the_lakehouse_descriptor_changes_the_revision(external_project: Path) -> None:
    before = _build(external_project)

    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    descriptor.write_text(descriptor.read_text() + "\n# a harmless trailing comment\n")

    after = _build(external_project)

    assert before.revision != after.revision


def test_editing_the_deployment_profile_does_not_change_the_revision(external_project: Path) -> None:
    before = _build(external_project)

    profile = external_project / "openlakeforge.yaml"
    profile.write_text(profile.read_text() + "\n# deployment intent is not project content\n")

    after = _build(external_project)

    assert before.revision == after.revision


def test_build_refuses_an_invalid_project(external_project: Path) -> None:
    (external_project / "lakehouse_code" / "lakehouse.yaml").unlink()

    with pytest.raises(project_revision.ProjectRevisionError, match="is not valid"):
        _build(external_project)


def test_build_rejects_a_bare_mutable_image_tag(external_project: Path) -> None:
    with pytest.raises(project_revision.ProjectRevisionError):
        project_revision.build_project_revision(
            _spec(external_project),
            image="ghcr.io/openlakeforge/openlakeforge-project-code:local",
            distribution_version=_DISTRIBUTION_VERSION,
        )


def test_build_resolves_a_mutable_tag_through_the_injected_resolver(external_project: Path) -> None:
    resolved = "ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "b" * 64
    manifest = project_revision.build_project_revision(
        _spec(external_project),
        image="ghcr.io/openlakeforge/openlakeforge-project-code:local",
        distribution_version=_DISTRIBUTION_VERSION,
        resolve_image_digest_with=lambda image: resolved,  # noqa: ARG005
    )

    assert manifest.project_code_image == resolved


def test_manifest_contains_no_stage_bound_values(external_project: Path) -> None:
    manifest = _build(external_project)

    rendered = manifest.to_json()
    for forbidden in ("s3://", "http://", "https://"):
        assert forbidden not in rendered


def test_manifest_json_round_trips(external_project: Path) -> None:
    manifest = _build(external_project)

    restored = project_revision.ProjectRevisionManifest.from_json(manifest.to_json())

    assert restored == manifest


def test_publish_and_verify_a_revision(external_project: Path, tmp_path: Path) -> None:
    manifest = _build(external_project)
    store = FilesystemRevisionStore(tmp_path / "store")

    project_revision.publish(store, manifest, _spec(external_project))
    verified = project_revision.verify(store, manifest.revision, running_distribution_version=_DISTRIBUTION_VERSION)

    assert verified == manifest


def test_publish_is_idempotent(external_project: Path, tmp_path: Path) -> None:
    manifest = _build(external_project)
    store = FilesystemRevisionStore(tmp_path / "store")

    project_revision.publish(store, manifest, _spec(external_project))
    project_revision.publish(store, manifest, _spec(external_project))  # no error on repeat

    assert project_revision.inspect(store, manifest.revision) == manifest


def test_verify_detects_a_tampered_object(external_project: Path, tmp_path: Path) -> None:
    manifest = _build(external_project)
    store = FilesystemRevisionStore(tmp_path / "store")
    project_revision.publish(store, manifest, _spec(external_project))

    descriptors = manifest.component("descriptors")
    assert descriptors is not None
    tampered_key = next(iter(descriptors.entries))
    prefix = project_revision.revision_prefix(manifest.revision)
    (tmp_path / "store" / f"{prefix}/descriptors/{tampered_key}").write_bytes(b"tampered")

    with pytest.raises(project_revision.ProjectRevisionError, match="hashes to"):
        project_revision.verify(store, manifest.revision)


def test_verify_detects_partial_publication(external_project: Path, tmp_path: Path) -> None:
    manifest = _build(external_project)
    store = FilesystemRevisionStore(tmp_path / "store")
    project_revision.publish(store, manifest, _spec(external_project))

    descriptors = manifest.component("descriptors")
    assert descriptors is not None
    missing_key = next(iter(descriptors.entries))
    prefix = project_revision.revision_prefix(manifest.revision)
    (tmp_path / "store" / f"{prefix}/descriptors/{missing_key}").unlink()

    with pytest.raises(project_revision.ProjectRevisionError, match="missing"):
        project_revision.verify(store, manifest.revision)


def test_verify_fails_closed_on_an_incompatible_distribution(external_project: Path, tmp_path: Path) -> None:
    manifest = _build(external_project)
    store = FilesystemRevisionStore(tmp_path / "store")
    project_revision.publish(store, manifest, _spec(external_project))

    with pytest.raises(project_revision.ProjectRevisionError, match="incompatible"):
        project_revision.verify(store, manifest.revision, running_distribution_version="9.9.9-alpha.1")


def test_inspect_rejects_a_sidecar_declaring_a_different_revision(external_project: Path, tmp_path: Path) -> None:
    manifest = _build(external_project)
    store = FilesystemRevisionStore(tmp_path / "store")
    other_revision = "sha256:" + ("1" if manifest.revision[-1] != "1" else "2") * 64
    store.write(
        project_revision.sidecar_key(other_revision),
        manifest.to_json().encode("utf-8"),
        content_type="application/json",
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="not requested"):
        project_revision.inspect(store, other_revision)


def test_validate_rejects_a_top_level_image_field_that_drifted_from_its_component(
    external_project: Path,
) -> None:
    manifest = _build(external_project)
    tampered = project_revision.ProjectRevisionManifest(
        project_name=manifest.project_name,
        distribution_version=manifest.distribution_version,
        project_code_image="ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "9" * 64,
        components=manifest.components,
        revision=manifest.revision,
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="does not match the manifest's image"):
        project_revision.ProjectRevisionManifest.from_json(tampered.to_json())


def test_validate_rejects_a_top_level_distribution_field_that_drifted_from_its_component(
    external_project: Path,
) -> None:
    manifest = _build(external_project)
    tampered = project_revision.ProjectRevisionManifest(
        project_name=manifest.project_name,
        distribution_version="9.9.9-alpha.1",
        project_code_image=manifest.project_code_image,
        components=manifest.components,
        revision=manifest.revision,
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="does not match the manifest's distribution"):
        project_revision.ProjectRevisionManifest.from_json(tampered.to_json())


def test_build_rejects_a_component_file_containing_a_stage_bound_value(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    descriptor.write_text(descriptor.read_text() + "\n# s3://leaked-ops-bucket/floe/reports\n")

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_publish_fails_closed_when_a_source_file_changes_after_build(
    external_project: Path, tmp_path: Path
) -> None:
    manifest = _build(external_project)
    store = FilesystemRevisionStore(tmp_path / "store")

    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    descriptor.write_text(descriptor.read_text() + "\n# drifted after build, before publish\n")

    with pytest.raises(project_revision.ProjectRevisionError, match="changed on disk since the revision was built"):
        project_revision.publish(store, manifest, _spec(external_project))

    # Nothing under this revision's prefix should be readable -- publish must
    # not have written the drifted bytes under a sidecar that still claims
    # the original digest.
    assert store.read(project_revision.sidecar_key(manifest.revision)) is None


def test_resolve_image_digest_rejects_a_local_config_id_without_a_repo_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from olf.deployment import engine as engine_module
    from olf.tooling.process import CommandResult

    class _FakeDocker:
        def image_inspect(self, image: str, *, check: bool = False) -> CommandResult:  # noqa: ARG002
            import json as _json

            payload = _json.dumps([{"Id": "sha256:" + "0" * 64, "RepoDigests": []}])
            return CommandResult(argv=("docker",), returncode=0, stdout=payload, stderr="", duration_seconds=0.0)

    class _FakeToolkit:
        docker = _FakeDocker()

    monkeypatch.setattr(engine_module.Toolkit, "default", classmethod(lambda cls: _FakeToolkit()))

    with pytest.raises(project_revision.ProjectRevisionError, match="no registry digest"):
        project_revision.resolve_image_digest("ghcr.io/openlakeforge/openlakeforge-project-code:local")


def test_validate_rejects_duplicate_component_names(external_project: Path) -> None:
    manifest = _build(external_project)
    image_component = manifest.component("image")
    assert image_component is not None
    forged_image = "ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "5" * 64
    tampered = project_revision.ProjectRevisionManifest(
        project_name=manifest.project_name,
        distribution_version=manifest.distribution_version,
        project_code_image=forged_image,
        # Two "image" components: a forged one matching the top-level field
        # (first, read by component()) and the original (last, read by the
        # digest aggregation) -- this must be rejected outright rather than
        # silently picking one or the other.
        components=(
            project_revision.ComponentEntries("image", {"project-code": forged_image}),
            *manifest.components,
        ),
        revision=manifest.revision,
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="duplicate component name"):
        project_revision.ProjectRevisionManifest.from_json(tampered.to_json())


def test_build_does_not_reject_a_referenced_secret_name_or_env_var_name(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(
        original
        + "\n# references only, not values: AWS_REGION, AWS_SECRET_ACCESS_KEY, secret_name: seaweedfs-s3-creds\n"
    )

    manifest = _build(external_project)  # must not raise

    assert manifest.revision.startswith("sha256:")


def test_build_does_not_reject_a_legitimate_external_https_reference(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + "\n# see https://example.com/docs for background\n")

    manifest = _build(external_project)  # must not raise

    assert manifest.revision.startswith("sha256:")


def test_build_rejects_an_in_cluster_http_endpoint(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + "\n# http://seaweedfs-s3:8333\n")

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_rejects_a_concrete_aws_access_key_id(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + "\n# AKIAABCDEFGHIJKLMNOP\n")

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_rejects_a_concrete_credential_assignment(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + '\n# password: production-password\n')

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_rejects_a_concrete_api_token_assignment(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + '\n# api_token = "sk-abc123XYZ456"\n')

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_still_allows_secret_and_client_secret_key_references(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(
        original
        + "\n# references, not assignments: secret_name: seaweedfs-s3-creds, "
        "secretRef: polaris-floe-creds, client_secret_key: POLARIS_FLOE_CLIENT_SECRET\n"
    )

    manifest = _build(external_project)  # must not raise

    assert manifest.revision.startswith("sha256:")


def test_validate_rejects_a_self_consistent_sidecar_with_a_mutable_image(external_project: Path) -> None:
    manifest = _build(external_project)
    mutable_image = "ghcr.io/openlakeforge/openlakeforge-project-code:latest"
    image_entries = dict(manifest.component("image").entries)
    image_entries["project-code"] = mutable_image
    components = tuple(
        project_revision.ComponentEntries("image", image_entries) if c.name == "image" else c
        for c in manifest.components
    )
    tampered = project_revision.ProjectRevisionManifest(
        project_name=manifest.project_name,
        distribution_version=manifest.distribution_version,
        project_code_image=mutable_image,
        components=components,
        revision=project_revision._aggregate_components(components),
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="not digest-pinned"):
        project_revision.ProjectRevisionManifest.from_json(tampered.to_json())


def test_dbt_component_excludes_local_artifacts(external_project: Path) -> None:
    dbt_root = external_project / "lakehouse_code/gold/order_revenue/dbt"
    (dbt_root / "logs").mkdir(parents=True, exist_ok=True)
    (dbt_root / "logs/dbt.log").write_text("http://seaweedfs-s3:8333 would trip the scanner if included\n")
    (dbt_root / ".user.yml").write_text("id: local-only\n")
    (dbt_root / "package-lock.yml").write_text("packages: []\n")

    manifest = _build(external_project)  # must not raise, and must not include the local artifacts

    dbt_keys = manifest.component("dbt").entries
    assert not any("logs/" in key for key in dbt_keys)
    assert not any(key.endswith(".user.yml") for key in dbt_keys)
    assert not any(key.endswith("package-lock.yml") for key in dbt_keys)


def test_dbt_local_artifacts_do_not_change_the_revision(external_project: Path) -> None:
    before = _build(external_project).revision

    dbt_root = external_project / "lakehouse_code/gold/order_revenue/dbt"
    (dbt_root / "logs").mkdir(parents=True, exist_ok=True)
    (dbt_root / "logs/dbt.log").write_text("ran locally\n")
    (dbt_root / "package-lock.yml").write_text("packages: []\n")

    after = _build(external_project).revision

    assert before == after


def test_build_rejects_a_duplicate_floe_config_in_one_domain(external_project: Path) -> None:
    floe_dir = external_project / "lakehouse_code/silver/sales/contracts/floe"
    original = (floe_dir / "sales.yml").read_text()
    (floe_dir / "sales-duplicate.yml").write_text(original)

    with pytest.raises(project_revision.ProjectRevisionError, match="one Floe configuration"):
        _build(external_project)


def test_build_allows_python_environment_lookups(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(
        original + '\n# token = os.environ["API_TOKEN"]\n# password = os.getenv("DB_PASSWORD")\n'
    )

    manifest = _build(external_project)  # must not raise

    assert manifest.revision.startswith("sha256:")


def test_build_allows_shell_style_env_substitution(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + "\n# password: ${DB_PASSWORD}\n")

    manifest = _build(external_project)  # must not raise

    assert manifest.revision.startswith("sha256:")


def test_build_rejects_a_quoted_json_style_credential_assignment(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + '\n# "password": "production-password"\n')

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_rejects_a_symlinked_component_file(external_project: Path, tmp_path: Path) -> None:
    outside_secret = tmp_path / "outside-the-project" / "host-secret.txt"
    outside_secret.parent.mkdir(parents=True)
    outside_secret.write_text("not part of any tracked project\n")

    descriptor = external_project / "lakehouse_code/bronze/crm/dlt/crm.py"
    descriptor.unlink()
    descriptor.symlink_to(outside_secret)

    with pytest.raises(project_revision.ProjectRevisionError, match="symlink"):
        _build(external_project)


def test_build_rejects_a_path_escaping_the_project_root_via_a_symlinked_directory(
    external_project: Path, tmp_path: Path
) -> None:
    import shutil

    # rglob does not recurse into a symlink it *encounters while walking* --
    # confirmed separately -- so the leak vector is specifically a component
    # *root itself* being a symlink: `_walk_files`'s `root.is_dir()` follows
    # it, and `root.rglob("*")` then happily enumerates the target's real
    # files. Reproduce that: keep dbt_project.yml etc. present (so
    # validate_project still passes) by copying the real dbt dir out, then
    # replace it with a symlink to the copy.
    outside_dir = tmp_path / "outside-the-project" / "dbt"
    dbt_root = external_project / "lakehouse_code/gold/order_revenue/dbt"
    shutil.copytree(dbt_root, outside_dir)
    (outside_dir / "host-file.sql").write_text("select 1\n")
    shutil.rmtree(dbt_root)
    dbt_root.symlink_to(outside_dir)

    with pytest.raises(project_revision.ProjectRevisionError, match="symlink|outside the project root"):
        _build(external_project)


def test_inspect_rejects_a_sidecar_with_a_null_components_field(tmp_path: Path) -> None:
    store = FilesystemRevisionStore(tmp_path / "store")
    revision = "sha256:" + "7" * 64
    store.write(
        project_revision.sidecar_key(revision),
        (
            '{"apiVersion": "openlakeforge.io/v1alpha1", "kind": "ProjectRevision", '
            '"schema_version": 1, "project_name": "demo", "distribution_version": "0.0.0", '
            '"project_code_image": "repo@sha256:' + "a" * 64 + '", "components": null, '
            '"revision": "' + revision + '"}'
        ).encode("utf-8"),
        content_type="application/json",
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="must be an array"):
        project_revision.inspect(store, revision)


def test_validate_rejects_a_sidecar_missing_required_components(external_project: Path) -> None:
    manifest = _build(external_project)
    kept = tuple(c for c in manifest.components if c.name in {"image", "distribution"})
    tampered = project_revision.ProjectRevisionManifest(
        project_name=manifest.project_name,
        distribution_version=manifest.distribution_version,
        project_code_image=manifest.project_code_image,
        components=kept,
        revision=project_revision._aggregate_components(kept),
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="missing required component"):
        project_revision.ProjectRevisionManifest.from_json(tampered.to_json())


def test_validate_rejects_a_sidecar_with_an_unknown_component_name(external_project: Path) -> None:
    manifest = _build(external_project)
    components = (*manifest.components, project_revision.ComponentEntries("mystery", {"a": "0" * 64}))
    tampered = project_revision.ProjectRevisionManifest(
        project_name=manifest.project_name,
        distribution_version=manifest.distribution_version,
        project_code_image=manifest.project_code_image,
        components=components,
        revision=project_revision._aggregate_components(components),
    )

    with pytest.raises(project_revision.ProjectRevisionError, match="unknown component"):
        project_revision.ProjectRevisionManifest.from_json(tampered.to_json())


def test_resolve_image_digest_selects_the_repo_digest_matching_the_requested_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from olf.deployment import engine as engine_module
    from olf.tooling.process import CommandResult

    wrong_repo_digest = "docker.io/someone-else/project-code@sha256:" + "1" * 64
    right_repo_digest = "ghcr.io/openlakeforge/openlakeforge-project-code@sha256:" + "2" * 64

    class _FakeDocker:
        def image_inspect(self, image: str, *, check: bool = False) -> CommandResult:  # noqa: ARG002
            import json as _json

            payload = _json.dumps(
                [{"Id": "sha256:" + "0" * 64, "RepoDigests": [wrong_repo_digest, right_repo_digest]}]
            )
            return CommandResult(argv=("docker",), returncode=0, stdout=payload, stderr="", duration_seconds=0.0)

    class _FakeToolkit:
        docker = _FakeDocker()

    monkeypatch.setattr(engine_module.Toolkit, "default", classmethod(lambda cls: _FakeToolkit()))

    resolved = project_revision.resolve_image_digest("ghcr.io/openlakeforge/openlakeforge-project-code:local")

    assert resolved == right_repo_digest


def test_resolve_image_digest_fails_when_no_repo_digest_matches_the_requested_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from olf.deployment import engine as engine_module
    from olf.tooling.process import CommandResult

    class _FakeDocker:
        def image_inspect(self, image: str, *, check: bool = False) -> CommandResult:  # noqa: ARG002
            import json as _json

            payload = _json.dumps(
                [
                    {
                        "Id": "sha256:" + "0" * 64,
                        "RepoDigests": ["docker.io/someone-else/project-code@sha256:" + "1" * 64],
                    }
                ]
            )
            return CommandResult(argv=("docker",), returncode=0, stdout=payload, stderr="", duration_seconds=0.0)

    class _FakeToolkit:
        docker = _FakeDocker()

    monkeypatch.setattr(engine_module.Toolkit, "default", classmethod(lambda cls: _FakeToolkit()))

    with pytest.raises(project_revision.ProjectRevisionError, match="no registry digest"):
        project_revision.resolve_image_digest("ghcr.io/openlakeforge/openlakeforge-project-code:local")


def test_build_rejects_a_default_argument_smuggled_into_an_env_lookup(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + '\n# token: os.getenv("API_TOKEN", "sk-live-abc123XYZ")\n')

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_rejects_a_default_argument_smuggled_into_a_dotted_env_lookup(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + '\n# password = os.environ.get("DB_PASSWORD", "changeme123")\n')

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_rejects_a_default_smuggled_into_shell_style_substitution(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(original + "\n# password: ${DB_PASSWORD:-hunter2changeme}\n")

    with pytest.raises(project_revision.ProjectRevisionError, match="stage-bound value"):
        _build(external_project)


def test_build_still_allows_a_bare_env_lookup_with_no_default(external_project: Path) -> None:
    descriptor = external_project / "lakehouse_code/lakehouse.yaml"
    original = descriptor.read_text()
    descriptor.write_text(
        original
        + '\n# token: os.getenv("API_TOKEN")\n'
        + '# password = os.environ.get("DB_PASSWORD")\n'
        + "# password: ${DB_PASSWORD}\n"
    )

    manifest = _build(external_project)  # must not raise

    assert manifest.revision.startswith("sha256:")


def test_registry_host_only_matches_provider_native_registries() -> None:
    """A revision may live outside the provider's registry; logging in there would fail."""
    from olf.deployment.activation import _registry_host

    ecr = "883553345052.dkr.ecr.eu-west-3.amazonaws.com/openlakeforge/project-code"
    assert _registry_host(ecr) == "883553345052.dkr.ecr.eu-west-3.amazonaws.com"
    assert _registry_host("ghcr.io/example/project-code") == "ghcr.io"
    assert _registry_host("localhost:5001/project-code") == "localhost:5001"
    # Docker Hub short form carries no host segment.
    assert _registry_host("openlakeforge/project-code") == ""
    assert _registry_host(ecr) != _registry_host("ghcr.io/example/project-code")


def test_ambient_registry_env_restores_the_callers_docker_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A private GHCR pull can only use credentials `docker login` wrote to the caller's config."""
    from olf.tooling.docker import ambient_registry_env

    scoped = {"DOCKER_CONFIG": "/work/.tmp/docker/local", "AWS_REGION": "eu-west-3"}

    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    assert ambient_registry_env(scoped)["DOCKER_CONFIG"] == "/home/dev/.docker"

    # With no ambient override, Docker's own default (~/.docker) must apply,
    # which means removing the scoped value rather than keeping it.
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    resolved = ambient_registry_env(scoped)
    assert "DOCKER_CONFIG" not in resolved
    assert resolved["AWS_REGION"] == "eu-west-3"


def test_build_rejects_a_report_bundle_with_a_dangling_identity(external_project: Path) -> None:
    chart = next((external_project / "lakehouse_code/dashboards/superset").glob("*/charts/*.yaml"))
    chart.unlink()

    with pytest.raises(project_revision.ProjectRevisionError, match="not promotable"):
        _build(external_project)
