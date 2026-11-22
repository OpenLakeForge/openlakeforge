from __future__ import annotations

import signal
import time
from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.common.deployment.context import DeploymentContext, DeploymentFeatures, Profile
from olf.common.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.release._manifest import load_catalog
from olf.common.tooling.process import CommandResult
from olf.common.tooling.resolver import PathExecutableResolver
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment.local import prefetch
from olf.infra.deployment.local.images import containerd_name, split_digest_reference

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CATALOG = load_catalog(_REPO_ROOT / "release" / "component-catalog.yaml")["components"]["images"]


def _config(tmp_path: Path, *, profile: Profile = Profile.FULL) -> LocalDeploymentConfig:
    context = DeploymentContext.local(repo_root=tmp_path, profile=profile, distribution_root=_REPO_ROOT)
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.common.tooling.docker import Docker
    from olf.common.tooling.helm import Helm
    from olf.common.tooling.kind import Kind
    from olf.common.tooling.kubectl import Kubectl
    from olf.common.tooling.terraform import Terraform

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
    )


def test_selected_images_full_profile_includes_governance_and_analytics() -> None:
    features = DeploymentFeatures(governance_enabled=True, analytics_enabled=True)
    images = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG)

    assert _CATALOG["opensearch"] in images
    assert any(i.startswith("apache/polaris:1.9.0@sha256:5dabc353") for i in images)
    assert any(i.startswith("apache/polaris-admin-tool:1.9.0@sha256:d6adc43e") for i in images)
    assert "docker.io/openmetadata/server:1.13.6" in images
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
    assert any(i.startswith("apache/polaris:1.9.0") for i in images)
    # Floe runs in-process in the project-code image; there is no runner image to load.
    assert not any("floe" in image for image in images)
    assert any(image.startswith("postgres:18.7-alpine@sha256:0115267a") for image in images)


def test_dex_is_prefetched_only_for_the_github_issuer() -> None:
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)

    plain = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG)
    with_dex = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG, dex=True)
    assert not any("dexidp/dex" in image for image in plain)
    assert any(image.startswith("ghcr.io/dexidp/dex:") for image in with_dex)


def test_the_metrics_images_are_prefetched_only_with_metrics() -> None:
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)

    without = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG)
    with_metrics = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG, metrics=True)

    metrics_images = {_CATALOG["prometheus"], _CATALOG["kube_state_metrics"]}
    assert not metrics_images & set(without)
    assert set(with_metrics) - set(without) == metrics_images


@pytest.mark.parametrize(("profile", "pulled"), [(Profile.FULL, True), (Profile.SLIM, False)])
def test_prefetch_follows_the_profiles_metrics_setting(tmp_path: Path, profile: Profile, pulled: bool) -> None:
    config = _config(tmp_path, profile=profile)
    runner = _ScriptedRunner()

    prefetch.prefetch_images(config, _toolkit(runner), env={}, work_dir=tmp_path / "scratch")

    pulls = [c.argv for c in runner.calls if "crictl" in c.argv and "pull" in c.argv]
    for name in ("prometheus", "kube_state_metrics"):
        assert any(_CATALOG[name] in argv for argv in pulls) is pulled


def test_polaris_images_arch_mapping() -> None:
    arm_image, arm_admin = prefetch.polaris_images("arm64", _CATALOG)
    assert arm_image.startswith("apache/polaris:1.9.0@sha256:14bcb2f3")
    assert arm_admin.startswith("apache/polaris-admin-tool:1.9.0@sha256:d89e2bc1")

    amd_image, amd_admin = prefetch.polaris_images("x86_64", _CATALOG)
    assert amd_image.startswith("apache/polaris:1.9.0@sha256:5dabc353")

    fallback_image, fallback_admin = prefetch.polaris_images("riscv64", _CATALOG)
    assert fallback_image == _CATALOG["polaris"]
    assert fallback_admin.startswith("apache/polaris-admin-tool:1.9.0@sha256:9ec24676")


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


class _IncompleteDockerStore(_ScriptedRunner):
    """Docker before 29.9 holding an image whose blobs its pull skipped: the save, or each node's import, fails."""

    def __init__(self, *, fail: str, node_pull_ok: bool = True) -> None:
        super().__init__()
        self._fail = fail
        self._node_pull_ok = node_pull_ok

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        failing = (self._fail == "save" and argv[1:2] == ["save"]) or (
            self._fail == "import" and "ctr" in argv and "import" in argv
        )
        if failing:
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            raise CommandExecutionError(argv, 1, stderr="content digest sha256:a635e3d8: not found")
        if "crictl" in argv and "pull" in argv and not self._node_pull_ok:
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            return CommandResult(argv=(), returncode=1, stdout="", stderr="EOF", duration_seconds=0.0)
        return super().run(command, **kwargs)


@pytest.mark.parametrize("fail", ["save", "import"])
def test_an_image_docker_cannot_archive_is_pulled_by_the_node_at_its_pin(tmp_path: Path, fail: str) -> None:
    runner = _IncompleteDockerStore(fail=fail)
    image = _CATALOG["kube_state_metrics"]

    prefetch.load_image_into_nodes(_config(tmp_path, profile=Profile.SLIM), _toolkit(runner), image, env={})

    node_pulls = [c.argv for c in runner.calls if "crictl" in c.argv and "pull" in c.argv]
    assert node_pulls == [["docker", "exec", "node-a", "crictl", "pull", image]]


def test_an_image_neither_docker_nor_the_node_can_provide_fails_naming_the_docker_fix(tmp_path: Path) -> None:
    runner = _IncompleteDockerStore(fail="save", node_pull_ok=False)
    image = _CATALOG["kube_state_metrics"]

    with pytest.raises(DeploymentPreconditionError, match="Docker Engine 29.9"):
        prefetch.load_image_into_nodes(_config(tmp_path, profile=Profile.SLIM), _toolkit(runner), image, env={})


def test_prefetch_images_skips_images_already_present_on_every_node(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    images = prefetch.selected_images(config.features, server_arch="amd64", catalog_images=_CATALOG)
    runner = _ScriptedRunner(image_already_present=set(images))
    tools = _toolkit(runner)

    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")

    assert not any(c.argv[:2] == ["docker", "save"] for c in runner.calls)
    assert not any(c.argv[:2] == ["docker", "pull"] for c in runner.calls)


def test_prefetch_pulls_catalog_images_in_the_node_not_through_the_host(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    runner = _ScriptedRunner(image_already_present=set())

    prefetch.prefetch_images(config, _toolkit(runner), env={}, work_dir=tmp_path / "scratch")

    # A `docker save` import would keep uncompressed blobs the node never drops (#1373).
    polaris_image = prefetch.polaris_images("amd64", _CATALOG)[0]
    assert ["docker", "exec", "node-a", "crictl", "pull", polaris_image] in [c.argv for c in runner.calls]
    host_verbs = {c.argv[1] for c in runner.calls if c.argv[0] == "docker"}
    assert not host_verbs & {"pull", "save", "tag"}
    assert not any("import" in c.argv and "ctr" in c.argv for c in runner.calls)


def test_an_image_the_host_already_holds_skips_the_node_pull(tmp_path: Path) -> None:
    class _HostHasImages(_ScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            if argv[1:3] == ["image", "inspect"]:
                self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
                return CommandResult(argv=(), returncode=0, stdout="", stderr="", duration_seconds=0.0)
            return super().run(command, **kwargs)

    runner = _HostHasImages()

    prefetch.prefetch_images(_config(tmp_path, profile=Profile.SLIM), _toolkit(runner), env={}, work_dir=tmp_path)

    assert not any("crictl" in c.argv and "pull" in c.argv for c in runner.calls)
    assert any("import" in c.argv and "ctr" in c.argv for c in runner.calls)


class _OfflineNodeRunner(_ScriptedRunner):
    """A kind node with no egress: `crictl pull` fails, and only what was imported or tagged is resolvable."""

    def __init__(self) -> None:
        super().__init__()
        self.node_images: set[str] = set()
        self._saved = ""

    @staticmethod
    def _cri_name(reference: str) -> str:
        if "@" in reference:
            repository, digest = split_digest_reference(reference)
            return containerd_name(f"{repository}@{digest}")
        return containerd_name(reference)

    def run(self, command, **kwargs):  # type: ignore[override]
        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        fail = CommandResult(argv=(), returncode=1, stdout="", stderr="EOF", duration_seconds=0.0)
        if argv[1:2] == ["save"]:
            self._saved = argv[2]
        elif "crictl" in argv and "pull" in argv:
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            return fail
        elif "inspecti" in argv:
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            present = self._cri_name(argv[-1]) in self.node_images
            return CommandResult(argv=(), returncode=0 if present else 1, stdout="", stderr="", duration_seconds=0.0)
        elif "ctr" in argv and "import" in argv:
            # `docker save` of an untagged name archives it as `:latest`, the name the import then records.
            saved = self._saved if ":" in self._saved.rsplit("/", 1)[-1] else f"{self._saved}:latest"
            self.node_images.add(containerd_name(saved))
        elif "ctr" in argv and "tag" in argv:
            source, target = argv[-2:]
            if source not in self.node_images:
                return fail
            self.node_images.add(target)
        return super().run(command, **kwargs)


def test_digest_pinned_prefetch_needs_no_registry_from_the_node(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    runner = _OfflineNodeRunner()
    tools = _toolkit(runner)

    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")

    # The node's pull failed, so the host's Docker supplied the image.
    polaris_image = prefetch.polaris_images("amd64", _CATALOG)[0]
    assert ["docker", "exec", "node-a", "crictl", "pull", polaris_image] in [c.argv for c in runner.calls]
    assert prefetch.image_present_on_all_nodes(tools.docker, polaris_image, ["node-a"], env={})
    # The names containerd records for the import and CRI resolves for the pin, written out rather than derived.
    digest = polaris_image.split("@", 1)[1]
    assert {"docker.io/apache/polaris:1.9.0", f"docker.io/apache/polaris@{digest}"} <= runner.node_images


def test_the_host_pull_is_the_failure_reported_when_neither_can_pull(tmp_path: Path) -> None:
    class _NothingPulls(_OfflineNodeRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            if argv[1:2] == ["pull"]:
                raise CommandExecutionError(argv, 1, stderr="toomanyrequests")
            return super().run(command, **kwargs)

    with pytest.raises(CommandExecutionError, match="toomanyrequests"):
        prefetch.prefetch_images(
            _config(tmp_path, profile=Profile.SLIM), _toolkit(_NothingPulls()), env={}, work_dir=tmp_path
        )


def test_a_digest_only_pin_is_tagged_from_the_latest_name_docker_gave_it(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    runner = _OfflineNodeRunner()
    tools = _toolkit(runner)
    image = "busybox@sha256:" + "a" * 64

    prefetch.load_image_into_nodes(config, tools, image, env={})

    assert f"docker.io/library/busybox@sha256:{'a' * 64}" in runner.node_images


def test_prefetch_images_does_not_retag_undigested_images(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    runner = _ScriptedRunner(image_already_present=set())
    tools = _toolkit(runner)

    prefetch.prefetch_images(config, tools, env={}, work_dir=tmp_path / "scratch")

    floe_tag_calls = [c for c in runner.calls if c.argv[:2] == ["docker", "tag"] and "floe" in " ".join(c.argv)]
    assert not floe_tag_calls


def test_canary_image_is_loaded_into_nodes_before_its_pods_are_created() -> None:
    from olf.infra.deployment.local import network_canary

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
    images = prefetch.selected_images(features, server_arch="amd64", catalog_images=_CATALOG, portal=True, dex=True)
    tags = {prefetch.archive_reference(i) for i in images}

    for name in (
        "seaweedfs", "traefik", "cert_manager_controller", "cert_manager_webhook", "cert_manager_cainjector",
        "postgres", "trino", "polaris", "polaris_admin_tool", "opensearch", "openmetadata_ingestion",
        "superset", "superset_init", "superset_redis", "oauth2_proxy", "dex", "cert_manager_startupapicheck", "portal",
        "dagster_busybox",
    ):  # fmt: skip
        assert prefetch.archive_reference(_CATALOG[name]) in tags, name
    # no preloaded tag drifts from the catalog; only the OpenMetadata server is pinned outside it
    pinned = {prefetch.archive_reference(v) for v in _CATALOG.values()}
    assert tags - pinned == {prefetch.archive_reference(prefetch._OPENMETADATA_SERVER_IMAGE)}


def test_a_locally_built_superset_is_not_prefetched() -> None:
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=True)
    built = prefetch.selected_images(features, server_arch="arm64", catalog_images=_CATALOG, build_superset=True)

    assert _CATALOG["superset"] not in built
    assert _CATALOG["superset_redis"] in built


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


def test_selected_images_loads_the_published_example_only_when_deployed() -> None:
    catalog = {**_CATALOG, "project_code": "ghcr.io/o/project-code:1@sha256:" + "b" * 64}
    features = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)

    assert catalog["project_code"] in prefetch.selected_images(
        features, server_arch="amd64", catalog_images=catalog, project_code=True
    )
    assert catalog["project_code"] not in prefetch.selected_images(
        features, server_arch="amd64", catalog_images=catalog
    )


def test_prefetch_pulls_into_workers_and_traefik_into_every_node(tmp_path: Path) -> None:
    nodes = ("openlakeforge-local-control-plane", "openlakeforge-local-worker", "openlakeforge-local-worker2")

    class _ThreeNodes(_ScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            if argv[1:3] == ["get", "nodes"]:
                return CommandResult(argv=(), returncode=0, stdout="\n".join(nodes), stderr="", duration_seconds=0.0)
            return super().run(command, **kwargs)

    runner = _ThreeNodes()
    config = _config(tmp_path, profile=Profile.SLIM)
    assert config.cluster.name == "openlakeforge-local"
    prefetch.prefetch_images(config, _toolkit(runner), env={}, work_dir=tmp_path)

    pulled = [c.argv[2] for c in runner.calls if "crictl" in c.argv and "pull" in c.argv]
    images = prefetch.selected_images(config.features, server_arch="amd64", catalog_images=_CATALOG, perimeter=False)
    assert pulled.count(nodes[1]) == pulled.count(nodes[2]) == len(images)
    assert pulled.count(nodes[0]) == 1  # Traefik only


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="the smoke deadline is a POSIX interval timer")
def test_smoke_deadline_interrupts_a_blocked_node_pull(tmp_path: Path) -> None:
    from olf.infra.smoke import SmokeError, _deadline

    class _BlockedPull(_ScriptedRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
            if "pull" in argv and "crictl" in argv:
                time.sleep(5)
            return super().run(command, **kwargs)

    started = time.monotonic()
    with pytest.raises(SmokeError), _deadline(1):
        prefetch.prefetch_images(
            _config(tmp_path, profile=Profile.SLIM), _toolkit(_BlockedPull()), env={}, work_dir=tmp_path
        )
    assert time.monotonic() - started < 3
