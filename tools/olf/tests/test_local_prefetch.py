from __future__ import annotations

from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.context import DeploymentContext, DeploymentFeatures, Profile
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.local import prefetch
from olf.deployment.local.config import LocalDeploymentConfig
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path, profile=profile)
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.tooling.aws import AwsCli
    from olf.tooling.azure import AzureCli
    from olf.tooling.docker import Docker
    from olf.tooling.helm import Helm
    from olf.tooling.kind import Kind
    from olf.tooling.kubectl import Kubectl
    from olf.tooling.terraform import Terraform

    resolver = PathExecutableResolver(
        overrides={t: Path(t) for t in ("terraform", "docker", "kind", "kubectl", "helm")}
    )
    return Toolkit(
        runner=runner,
        resolver=resolver,
        terraform=Terraform(runner, resolver),
        helm=Helm(runner, resolver),
        kubectl=Kubectl(runner, resolver),
        docker=Docker(runner, resolver),
        kind=Kind(runner, resolver),
        aws=AwsCli(runner, resolver),
        azure=AzureCli(runner, resolver),
    )


def test_selected_images_full_profile_includes_governance_and_analytics() -> None:
    features = DeploymentFeatures(governance_enabled=True, analytics_enabled=True)
    images = prefetch.selected_images(features, server_arch="amd64")

    assert images[0] == "opensearchproject/opensearch:2.11.0"
    assert images[1].startswith("apache/polaris:1.4.0@sha256:f4676e56")
    assert images[2].startswith("apache/polaris-admin-tool:1.4.0@sha256:3b13addc")
    assert "docker.getcollate.io/openmetadata/server:1.13.6" in images
    assert "docker.getcollate.io/openmetadata/ingestion-base:1.13.6" in images
    assert images[-1] == "ghcr.io/malon64/floe:0.6.11"
    assert "apache/superset:dockerize" in images
    assert "docker.io/bitnamilegacy/redis:7.0.10-debian-11-r4" in images


def test_selected_images_slim_profile_excludes_governance_and_analytics() -> None:
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)
    images = prefetch.selected_images(features, server_arch="amd64")

    assert "opensearchproject/opensearch:2.11.0" not in images
    assert not any("openmetadata" in image for image in images)
    assert not any("superset" in image for image in images)
    assert not any("redis" in image for image in images)
    assert images[0].startswith("apache/polaris:1.4.0")
    assert images[-1] == "ghcr.io/malon64/floe:0.6.11"
    assert any(image.startswith("postgres:16-alpine@sha256:57c72fd2") for image in images)


def test_keycloak_images_are_prefetched_unless_the_issuer_is_external() -> None:
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)

    default = prefetch.selected_images(features, server_arch="amd64")
    assert any(image.startswith("quay.io/keycloak/keycloak:") for image in default)
    assert any(image.startswith("adorsys/keycloak-config-cli:") for image in default)
    external = prefetch.selected_images(features, server_arch="amd64", keycloak=False)
    assert not any("keycloak" in image for image in external)


def test_polaris_images_arch_mapping() -> None:
    arm_image, arm_admin = prefetch.polaris_images("arm64")
    assert arm_image.startswith("apache/polaris:1.4.0@sha256:705f7c02")
    assert arm_admin.startswith("apache/polaris-admin-tool:1.4.0@sha256:c5313c03")

    amd_image, amd_admin = prefetch.polaris_images("x86_64")
    assert amd_image.startswith("apache/polaris:1.4.0@sha256:f4676e56")

    fallback_image, fallback_admin = prefetch.polaris_images("riscv64")
    assert fallback_image == "apache/polaris:1.4.0"  # no digest pin in the fallback branch
    assert fallback_admin.startswith("apache/polaris-admin-tool:1.4.0@sha256:7ef7557b")


def test_archive_reference_strips_digest() -> None:
    assert prefetch.archive_reference("apache/polaris:1.4.0@sha256:abcd") == "apache/polaris:1.4.0"
    assert prefetch.archive_reference("apache/superset:dockerize") == "apache/superset:dockerize"


def test_archive_filename_replaces_separators() -> None:
    assert prefetch.archive_filename("apache/polaris:1.4.0@sha256:abcd") == "apache_polaris_1.4.0_sha256_abcd.tar"


def test_prefetch_images_raises_when_cluster_has_no_nodes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = RecordingRunner(CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0))
    tools = _toolkit(runner)

    with pytest.raises(DeploymentPreconditionError):
        prefetch.prefetch_images(config, tools, env={})


class _ScriptedRunner(RecordingRunner):
    def __init__(self, image_already_present: set[str] | None = None) -> None:
        super().__init__()
        self._present = image_already_present or set()

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if argv[1:3] == ["get", "nodes"]:
            return CommandResult(argv=(), returncode=0, stdout="node-a\n", stderr="", duration_seconds=0.0)
        if argv[1:2] == ["version"]:
            return CommandResult(argv=(), returncode=0, stdout="amd64\n", stderr="", duration_seconds=0.0)
        if "inspecti" in argv:
            image = argv[-1]
            ok = image in self._present
            return CommandResult(argv=(), returncode=0 if ok else 1, stdout="", stderr="", duration_seconds=0.0)
        if argv[1:3] == ["image", "inspect"]:
            return CommandResult(argv=(), returncode=1, stdout="", stderr="", duration_seconds=0.0)
        return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)


def test_prefetch_images_skips_images_already_present_on_every_node(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    images = prefetch.selected_images(config.features, server_arch="amd64")
    runner = _ScriptedRunner(image_already_present=set(images))
    tools = _toolkit(runner)

    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")

    assert not any(c.argv[:2] == ["docker", "save"] for c in runner.calls)
    assert not any(c.argv[:2] == ["docker", "pull"] for c in runner.calls)


def test_prefetch_images_pulls_saves_and_imports_digest_pinned_image(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    runner = _ScriptedRunner(image_already_present=set())
    tools = _toolkit(runner)

    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")

    polaris_image = prefetch.polaris_images("amd64")[0]
    tag_calls = [c for c in runner.calls if c.argv[:2] == ["docker", "tag"] and c.argv[2] == polaris_image]
    assert len(tag_calls) == 1
    assert tag_calls[0].argv[3] == prefetch.archive_reference(polaris_image)

    save_calls = [c for c in runner.calls if c.argv[:2] == ["docker", "save"]]
    assert any(c.argv[2] == prefetch.archive_reference(polaris_image) for c in save_calls)

    import_calls = [c for c in runner.calls if "import" in c.argv and "ctr" in c.argv]
    assert import_calls  # loaded onto the single node
    assert import_calls[0].argv[1:4] == ["exec", "--privileged", "-i"]

    crictl_pull_calls = [
        c for c in runner.calls if c.argv[:2] == ["docker", "exec"] and c.argv[3:5] == ["crictl", "pull"]
    ]
    assert any(c.argv[-1] == polaris_image for c in crictl_pull_calls)


def test_prefetch_images_skips_crictl_pull_for_undigested_images(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    runner = _ScriptedRunner(image_already_present=set())
    tools = _toolkit(runner)

    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")

    floe_tag_calls = [c for c in runner.calls if c.argv[:2] == ["docker", "tag"] and "floe" in " ".join(c.argv)]
    assert not floe_tag_calls
