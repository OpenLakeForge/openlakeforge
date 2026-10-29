from __future__ import annotations

from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.context import DeploymentContext, DeploymentFeatures, Profile
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentPreconditionError
from olf.deployment.local import prefetch
from olf.deployment.local.config import LocalDeploymentConfig
from olf.release._manifest import load_catalog
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CATALOG = load_catalog(_REPO_ROOT / "release" / "component-catalog.yaml")["components"]["images"]


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path, profile=profile, distribution_root=_REPO_ROOT)
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
    images = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG)

    assert _CATALOG["opensearch"] in images
    assert any(i.startswith("apache/polaris:1.4.0@sha256:f4676e56") for i in images)
    assert any(i.startswith("apache/polaris-admin-tool:1.4.0@sha256:3b13addc") for i in images)
    assert "docker.getcollate.io/openmetadata/server:1.13.6" in images
    assert _CATALOG["openmetadata_ingestion"] in images
    # Floe runs in-process in the project-code image; there is no runner image to load.
    assert not any("floe" in image for image in images)
    assert "apache/superset:dockerize" in "".join(images)
    assert _CATALOG["superset_redis"] in images


def test_selected_images_slim_profile_excludes_governance_and_analytics() -> None:
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)
    images = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG)

    assert "opensearchproject/opensearch:3.3.2" not in images
    assert not any("openmetadata" in image for image in images)
    assert not any("superset" in image for image in images)
    assert not any("redis" in image for image in images)
    assert any(i.startswith("apache/polaris:1.4.0") for i in images)
    # Floe runs in-process in the project-code image; there is no runner image to load.
    assert not any("floe" in image for image in images)
    assert any(image.startswith("postgres:16-alpine@sha256:57c72fd2") for image in images)


def test_dex_is_prefetched_only_for_the_github_issuer() -> None:
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)

    plain = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG)
    with_dex = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG, dex=True)
    assert not any("dexidp/dex" in image for image in plain)
    assert any(image.startswith("ghcr.io/dexidp/dex:") for image in with_dex)


def test_polaris_images_arch_mapping() -> None:
    arm_image, arm_admin = prefetch.polaris_images("arm64", _CATALOG)
    assert arm_image.startswith("apache/polaris:1.4.0@sha256:705f7c02")
    assert arm_admin.startswith("apache/polaris-admin-tool:1.4.0@sha256:c5313c03")

    amd_image, amd_admin = prefetch.polaris_images("x86_64", _CATALOG)
    assert amd_image.startswith("apache/polaris:1.4.0@sha256:f4676e56")

    fallback_image, fallback_admin = prefetch.polaris_images("riscv64", _CATALOG)
    assert fallback_image == _CATALOG["polaris"]
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
    images = prefetch.selected_images(config.features, server_arch="amd64", catalog_images=_CATALOG)
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

    polaris_image = prefetch.polaris_images("amd64", _CATALOG)[0]
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


def test_canary_image_is_loaded_into_nodes_before_its_pods_are_created() -> None:
    from olf.deployment.local import network_canary

    class _StopAtCreate(Exception):
        pass

    class _Runner(_ScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            if "create" in argv:
                self.calls.append(RecordedCall(argv=["kubectl", "create"], kwargs={}))
                raise _StopAtCreate
            return super().run(command, **kwargs)

    runner = _Runner()
    with pytest.raises(_StopAtCreate):
        network_canary.require_policy_enforcement(_config(_REPO_ROOT), _toolkit(runner), env={})

    argvs = [c.argv for c in runner.calls]
    loaded = next(i for i, a in enumerate(argvs) if "import" in a and "ctr" in a)
    assert loaded < argvs.index(["kubectl", "create"])


def test_full_identity_selection_matches_the_catalog_pins() -> None:
    features = DeploymentFeatures(governance_enabled=True, analytics_enabled=True)
    images = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG, dex=True)
    tags = {prefetch.archive_reference(i) for i in images}

    for name in (
        "seaweedfs", "traefik", "cert_manager_controller", "cert_manager_webhook", "cert_manager_cainjector",
        "postgres", "trino", "polaris", "polaris_admin_tool", "opensearch", "openmetadata_ingestion",
        "superset", "superset_init", "superset_redis", "oauth2_proxy", "dex",
    ):  # fmt: skip
        assert prefetch.archive_reference(_CATALOG[name]) in tags, name
    # no preloaded tag drifts from the catalog; only the OpenMetadata server is pinned outside it
    pinned = {prefetch.archive_reference(v) for v in _CATALOG.values()}
    assert tags - pinned == {prefetch.archive_reference(prefetch._OPENMETADATA_SERVER_IMAGE)}


def test_prefetch_deletes_each_archive_once_every_node_loaded_it(tmp_path: Path) -> None:
    # The archives used to stay in the scratch dir until the run ended, which filled CI runners.
    config = _config(tmp_path, profile=Profile.SLIM)
    tools = _toolkit(_ScriptedRunner(image_already_present=set()))
    real_save = tools.docker.save

    def save(image: str, *, output_path: Path, env=None):  # noqa: ANN001, ANN202
        output_path.write_bytes(b"archive")
        return real_save(image, output_path=output_path, env=env)

    tools.docker.save = save  # type: ignore[method-assign]
    scratch = tmp_path / "scratch"
    prefetch.prefetch_images(config, tools, env={}, work_dir=scratch)

    assert list(scratch.iterdir()) == []
