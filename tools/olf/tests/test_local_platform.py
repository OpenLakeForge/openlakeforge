from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from _tooling_support import RecordedCall, RecordingRunner

from olf.deployment.charts import TERRAFORM_VARIABLE_KEY
from olf.deployment.context import DeploymentContext, Profile, Provider
from olf.deployment.engine import Toolkit
from olf.deployment.errors import CommandExecutionError, DeploymentPreconditionError
from olf.deployment.local import platform
from olf.deployment.local.config import LocalDeploymentConfig
from olf.tooling.process import CommandResult
from olf.tooling.resolver import PathExecutableResolver

_TOOLS = ("terraform", "docker", "kind", "kubectl", "helm")


def _topology(*, dev: bool = True, uat: bool = False, prod: bool = False, analytics: bool = True):  # noqa: ANN202
    from olf.profile import (
        DeploymentProfile,
        Preset,
        ProviderSpec,
        StageCapabilities,
        StageName,
        StageSpec,
        resolve_topology,
    )

    capabilities = StageCapabilities(analytics=analytics, governance=analytics)
    enabled = {StageName.DEV: dev, StageName.UAT: uat, StageName.PROD: prod}
    return resolve_topology(
        DeploymentProfile(
            name="acme-data",
            provider=ProviderSpec(type=Provider.LOCAL),
            preset=Preset.FULL,
            stages=tuple(
                StageSpec(name=name, enabled=is_enabled, capabilities=capabilities)
                for name, is_enabled in enabled.items()
            ),
        )
    )


def _config(
    tmp_path: Path,
    *,
    profile: Profile = Profile.FULL,
    topology=None,  # noqa: ANN001 - DeploymentTopology
    allow_stage_removal: bool = False,
) -> LocalDeploymentConfig:
    context = DeploymentContext.local(
        repo_root=tmp_path, profile=profile, topology=topology, allow_stage_removal=allow_stage_removal
    )
    return LocalDeploymentConfig.from_environment({}, context=context)


def _toolkit(runner: RecordingRunner) -> Toolkit:
    from olf.tooling.aws import AwsCli
    from olf.tooling.azure import AzureCli
    from olf.tooling.docker import Docker
    from olf.tooling.helm import Helm
    from olf.tooling.kind import Kind
    from olf.tooling.kubectl import Kubectl
    from olf.tooling.terraform import Terraform

    resolver = PathExecutableResolver(overrides={t: Path(t) for t in _TOOLS})
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


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(argv=(), returncode=0, stdout=stdout, stderr="", duration_seconds=0.0)


def _fail() -> CommandResult:
    return CommandResult(argv=(), returncode=1, stdout="", stderr="", duration_seconds=0.0)


def _fake_dagster_archive_bytes() -> bytes:
    """A minimal real gzip tarball shaped like `helm pull`'s Dagster output -
    see `test_deployment_charts.py`'s identical helper for why the schema-
    stripping repack needs a real archive rather than a `RecordingRunner`
    stub, even for a chart source mode never digest-verifies."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, content in (
            (b"dagster/Chart.yaml", b"name: dagster\n"),
            (b"dagster/values.schema.json", b"{}"),
        ):
            info = tarfile.TarInfo(name.decode())
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_platform_apply_variables_cover_every_root_input(tmp_path: Path) -> None:
    config = _config(tmp_path)

    variables = platform.platform_apply_variables(config)

    assert set(variables) == {
        "profile_name",
        "shared_namespace",
        "stages",
        "manage_user_deployments",
        "kube_context",
        "kubeconfig_path",
        "helm_repository_cache_path",
        "helm_repository_config_path",
        "foundation_state_path",
        "project_code_image_repository",
        "project_code_image_tag",
        "project_code_image_pull_policy",
        "project_code_image_revision",
        "superset_image_repository",
        "superset_image_tag",
        "superset_image_pull_policy",
        "trino_chart_package_path",
        "dagster_chart_package_path",
        "seaweedfs_chart_package_path",
        "polaris_chart_package_path",
        "openmetadata_chart_package_path",
        "openmetadata_deps_chart_package_path",
        "superset_chart_package_path",
        "traefik_chart_package_path",
        "cert_manager_chart_package_path",
        "access_base_domain",
        "access_issuer",
        "identity_issuer",
    }


def test_the_default_identity_issuer_deploys_keycloak_and_passes_no_claim_mapping(tmp_path: Path) -> None:
    variables = platform.platform_apply_variables(_config(tmp_path))

    assert variables["identity_issuer"] == "keycloak"
    assert "identity_external" not in variables


def test_an_external_issuer_carries_its_claim_mapping_into_the_root(tmp_path: Path) -> None:
    from dataclasses import replace

    from olf.profile import IdentitySpec

    external = IdentitySpec(
        issuer="external",
        issuer_url="https://idp.example.com/realms/acme",
        role_claim="roles",
        role_mapping={"platform-admin": ("olf-admins",), "viewer": ("olf-viewers",)},
    )
    topology = replace(_topology(), identity=external)

    variables = platform.platform_apply_variables(_config(tmp_path, topology=topology))

    assert variables["identity_issuer"] == "external"
    assert json.loads(variables["identity_external"]) == {
        "issuer_url": "https://idp.example.com/realms/acme",
        "role_claim": "roles",
        "role_mapping": {"platform-admin": ["olf-admins"], "viewer": ["olf-viewers"]},
    }


def test_platform_apply_variables_carry_the_resolved_topology(tmp_path: Path) -> None:
    config = _config(tmp_path)

    variables = platform.platform_apply_variables(config)

    assert variables["shared_namespace"] == "olf-system"
    assert json.loads(variables["stages"]) == {
        "dev": {"analytics": True, "enabled": True, "governance": True},
        "prod": {"analytics": False, "enabled": False, "governance": False},
        "uat": {"analytics": False, "enabled": False, "governance": False},
    }


def test_a_disabled_stage_is_still_reported_so_removal_is_visible(tmp_path: Path) -> None:
    """A stage the user turned off must stay in the map with `enabled: false`
    rather than disappearing: the root and the removal guard both need to see
    the difference between "never existed" and "was switched off"."""
    config = _config(tmp_path, topology=_topology(dev=True, prod=True))

    stages = json.loads(platform.platform_apply_variables(config)["stages"])

    assert stages["prod"]["enabled"] is True
    assert stages["uat"]["enabled"] is False


def test_platform_apply_variables_slim_profile_omits_disabled_layer_charts(tmp_path: Path) -> None:
    """Nothing fetches a chart a disabled layer will never install, and with
    no archive in the cache there is no path to pass for one either."""
    config = _config(tmp_path, profile=Profile.SLIM)

    variables = platform.platform_apply_variables(config)

    assert [setting.name for setting in config.charts.values()] == [
        "trino",
        "dagster",
        "seaweedfs",
        "polaris",
        "traefik",
        "cert-manager",
    ]
    assert "openmetadata_chart_package_path" not in variables
    assert "openmetadata_deps_chart_package_path" not in variables
    assert "superset_chart_package_path" not in variables
    assert "trino_chart_package_path" in variables
    assert "dagster_chart_package_path" in variables
    assert "seaweedfs_chart_package_path" in variables
    assert "polaris_chart_package_path" in variables


def test_apply_passes_cached_archives_for_optional_releases_the_topology_disabled(tmp_path: Path) -> None:
    """Turning off the last analytics stage is the apply that destroys
    Superset, and the capability gate no longer selects its chart. Without the
    cached archive the provider would fetch the repository index to remove the
    release, so stage removal would need the network."""
    config = _config(tmp_path, profile=Profile.SLIM)
    superset = config.charts["superset"]
    superset.package_path.parent.mkdir(parents=True, exist_ok=True)
    superset.package_path.write_bytes(b"chart archive")

    variables = platform.platform_apply_variables(config)

    assert variables["superset_chart_package_path"] == str(superset.package_path)


def test_platform_destroy_variables_are_the_four_var_subset(tmp_path: Path) -> None:
    config = _config(tmp_path)

    variables = platform.platform_destroy_variables(config)

    assert set(variables) == {
        "profile_name",
        "shared_namespace",
        "stages",
        "manage_user_deployments",
        "kube_context",
        "kubeconfig_path",
        "helm_repository_cache_path",
        "helm_repository_config_path",
        "foundation_state_path",
    }


def test_destroy_reuses_cached_chart_archives(tmp_path: Path) -> None:
    """Destroy must not need the chart repositories: a `helm_release` whose
    chart is a repository name makes the provider fetch that repo's index even
    to remove the release."""
    config = _config(tmp_path)
    cached = next(iter(config.charts.values()))
    cached.package_path.parent.mkdir(parents=True, exist_ok=True)
    cached.package_path.write_bytes(b"chart archive")

    variables = platform.platform_destroy_variables(config)

    assert variables[TERRAFORM_VARIABLE_KEY[cached.name]] == str(cached.package_path)


def test_destroy_omits_chart_archives_that_are_not_cached(tmp_path: Path) -> None:
    """A path the provider cannot open is worse than none: it rejects the
    missing file outright instead of falling back to the repository."""
    config = _config(tmp_path)

    variables = platform.platform_destroy_variables(config)

    assert not any(key.endswith("_chart_package_path") for key in variables)


def test_platform_var_files_are_only_ever_the_user_s_own(tmp_path: Path) -> None:
    """No preset selects a platform-owned var file any more: capabilities
    reach the root through the resolved topology."""
    full_config = _config(tmp_path, profile=Profile.FULL)
    slim_config = _config(tmp_path, profile=Profile.SLIM)
    user_config = LocalDeploymentConfig.from_environment(
        {"LOCAL_TFVARS_FILE": "custom.tfvars"}, context=DeploymentContext.local(repo_root=tmp_path)
    )

    assert platform.platform_var_files(full_config) == ()
    assert platform.platform_var_files(slim_config) == ()
    assert platform.platform_var_files(user_config) == (str(tmp_path / "custom.tfvars"),)


def test_platform_up_raises_when_foundation_state_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = _toolkit(RecordingRunner(_ok()))

    with pytest.raises(DeploymentPreconditionError, match="foundation Terraform state is missing"):
        platform.platform_up(config, tools, env={})


class _PlatformScriptedRunner(RecordingRunner):
    def __init__(
        self,
        *,
        namespace_exists: bool = False,
        legacy_namespace_exists: bool = False,
        seaweedfs_in_state: bool = True,
        apply_failures: int = 0,
    ) -> None:
        super().__init__()
        self._namespace_exists = namespace_exists
        self._legacy_namespace_exists = legacy_namespace_exists
        self._seaweedfs_in_state = seaweedfs_in_state
        self._apply_failures = apply_failures
        self._apply_attempts = 0

    def run(self, command, **kwargs):  # type: ignore[override]
        from olf.deployment.errors import CommandExecutionError

        argv = list(command.argv) if hasattr(command, "argv") else [str(p) for p in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))

        if "stage_names" in argv:
            # An applied root answers with its stage list; the removal guard
            # only treats an explicit "no such output" as "never applied".
            return _ok('["dev"]')

        if argv[0] == "kubectl" and "get-contexts" in argv:
            return _ok("kind-openlakeforge-local\n")
        if argv[0] == "kubectl" and "namespace" in argv and "get" in argv and "jsonpath" not in " ".join(argv):
            if "lakehouse" in argv:
                return _ok() if self._legacy_namespace_exists else _fail()
            return _ok() if self._namespace_exists else _fail()
        if argv[0] == "terraform" and "state" in argv and "module.seaweedfs.helm_release.seaweedfs" in argv:
            return _ok() if self._seaweedfs_in_state else _fail()
        if argv[0] == "terraform" and "apply" in argv:
            self._apply_attempts += 1
            if self._apply_attempts <= self._apply_failures:
                raise CommandExecutionError(argv, 1, stderr="apply failed")
            return _ok()
        if argv[0] == "helm" and "show" in argv:
            return CommandResult(argv=(), returncode=1, stdout="", stderr="", duration_seconds=0.0)
        if argv[0] == "helm" and "pull" in argv and "dagster/dagster" in argv:
            destination = Path(argv[argv.index("--destination") + 1])
            version = argv[argv.index("--version") + 1]
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"dagster-{version}.tgz").write_bytes(_fake_dagster_archive_bytes())
        if argv[0] == "helm" and "package" in argv:
            destination = Path(argv[argv.index("--destination") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "dagster-1.13.7.tgz").write_bytes(b"fake-repacked-chart")
        return _ok()


def test_platform_up_happy_path_applies_terraform(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    runner = _PlatformScriptedRunner(namespace_exists=False, seaweedfs_in_state=True)
    tools = _toolkit(runner)

    platform.platform_up(config, tools, env={})

    apply_calls = [c for c in runner.calls if c.argv[0] == "terraform" and "apply" in c.argv]
    assert len(apply_calls) == 1
    # The apply is not done until the local CA has actually signed a certificate.
    assert any("wait" in c.argv and "certificate/local-ca-probe" in c.argv for c in runner.calls)


def test_platform_up_recovers_from_drifted_state(tmp_path: Path) -> None:
    config = _config(tmp_path, profile=Profile.SLIM)
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    runner = _PlatformScriptedRunner(namespace_exists=True, seaweedfs_in_state=False)
    tools = _toolkit(runner)

    platform.platform_up(config, tools, env={})

    destroy_calls = [c for c in runner.calls if c.argv[0] == "terraform" and "destroy" in c.argv]
    assert len(destroy_calls) == 1  # teardown ran as part of drift recovery
    init_calls = [c for c in runner.calls if c.argv[0] == "terraform" and "init" in c.argv]
    assert len(init_calls) >= 2  # once up front, once after the drift-triggered teardown


def test_platform_up_retries_apply_and_cleans_up_polaris_jobs_each_attempt(tmp_path: Path) -> None:
    context = DeploymentContext.local(repo_root=tmp_path, profile=Profile.SLIM)
    config = LocalDeploymentConfig.from_environment(
        {"LOCAL_UP_RETRY_ATTEMPTS": "3", "LOCAL_UP_RETRY_DELAY_SECONDS": "0"}, context=context
    )
    config.paths.foundation_state_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.foundation_state_path.write_text("{}")
    runner = _PlatformScriptedRunner(namespace_exists=False, seaweedfs_in_state=True, apply_failures=1)
    tools = _toolkit(runner)

    platform.platform_up(config, tools, env={})

    apply_calls = [c for c in runner.calls if c.argv[0] == "terraform" and "apply" in c.argv]
    assert len(apply_calls) == 2  # first attempt failed, second succeeded


def _legacy_namespace_runner(*, legacy_namespace_exists: bool) -> RecordingRunner:
    """A runner whose `kubectl get namespace lakehouse` answers found/not-found."""

    class _Runner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(part) for part in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if "get" in argv and "namespace" in argv and "lakehouse" in argv:
                return _ok() if legacy_namespace_exists else _fail()
            return _ok()

    return _Runner()


def test_shared_namespace_replacement_fails_closed_when_the_legacy_namespace_exists(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True))
    tools = _toolkit(_legacy_namespace_runner(legacy_namespace_exists=True))

    with pytest.raises(DeploymentPreconditionError, match="lakehouse"):
        platform.require_no_shared_namespace_replacement(config, tools, kube_context=config.kube_context, env={})


def test_shared_namespace_replacement_is_allowed_with_the_explicit_opt_in(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True), allow_stage_removal=True)
    tools = _toolkit(_legacy_namespace_runner(legacy_namespace_exists=True))

    platform.require_no_shared_namespace_replacement(config, tools, kube_context=config.kube_context, env={})


def test_shared_namespace_replacement_is_a_no_op_on_a_fresh_cluster(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True))
    tools = _toolkit(_legacy_namespace_runner(legacy_namespace_exists=False))

    platform.require_no_shared_namespace_replacement(config, tools, kube_context=config.kube_context, env={})


def _stage_names_runner(applied: str | None, *, labelled_namespaces: str = "") -> RecordingRunner:
    """A runner whose `terraform output -json stage_names` answers `applied`,
    or fails the way an unapplied root does, and whose namespace label query
    answers `labelled_namespaces`."""

    class _Runner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(part) for part in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if "stage_names" in argv:
                if applied is None:
                    raise CommandExecutionError(argv, 1, stderr="No outputs found")
                return _ok(applied)
            if "namespace" in argv and "-l" in argv:
                return _ok(labelled_namespaces)
            return _ok()

    return _Runner()


def test_removing_an_applied_stage_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True))
    tools = _toolkit(_stage_names_runner('["dev", "prod"]'))

    with pytest.raises(DeploymentPreconditionError, match="prod"):
        platform.require_no_stage_removal(config, tools, kube_context=config.kube_context, env={})


def test_removing_an_applied_stage_is_allowed_with_the_explicit_opt_in(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True), allow_stage_removal=True)
    tools = _toolkit(_stage_names_runner('["dev", "prod"]'))

    platform.require_no_stage_removal(config, tools, kube_context=config.kube_context, env={})


def test_an_unapplied_root_has_no_stage_to_remove(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True))
    tools = _toolkit(_stage_names_runner(None))

    platform.require_no_stage_removal(config, tools, kube_context=config.kube_context, env={})


def test_adding_a_stage_is_not_a_removal(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True, prod=True))
    tools = _toolkit(_stage_names_runner('["dev"]'))

    platform.require_no_stage_removal(config, tools, kube_context=config.kube_context, env={})


def test_an_unreadable_state_does_not_read_as_no_stages(tmp_path: Path) -> None:
    """A guard that treats any `terraform output` failure as "no stages
    applied" waves through the very apply it exists to stop."""
    config = _config(tmp_path, topology=_topology(dev=True))

    class _Runner(RecordingRunner):
        def run(self, command, **kwargs):  # type: ignore[override]
            argv = list(command.argv) if hasattr(command, "argv") else [str(part) for part in command]
            self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
            if "stage_names" in argv:
                raise CommandExecutionError(argv, 1, stderr="Failed to load state: unable to open statefile")
            return _ok()

    with pytest.raises(CommandExecutionError):
        platform.require_no_stage_removal(
            config, _toolkit(_Runner()), kube_context=config.kube_context, env={}
        )


def test_a_dropped_stage_still_in_the_cluster_fails_closed_without_state(tmp_path: Path) -> None:
    """The drift path: state is missing, so `stage_names` reads as no applied
    stages, and `platform_up` would go straight to a reset whose teardown
    deletes by label -- taking a stage the profile dropped with it, under a
    guard that had nothing to read."""
    config = _config(tmp_path, topology=_topology(dev=True))
    tools = _toolkit(_stage_names_runner(None, labelled_namespaces="olf-system\nolf-dev\nolf-prod\n"))

    with pytest.raises(DeploymentPreconditionError, match="prod"):
        platform.require_no_stage_removal(config, tools, kube_context=config.kube_context, env={})


def test_a_dropped_stage_in_the_cluster_is_allowed_with_the_explicit_opt_in(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True), allow_stage_removal=True)
    tools = _toolkit(_stage_names_runner(None, labelled_namespaces="olf-system\nolf-dev\nolf-prod\n"))

    platform.require_no_stage_removal(config, tools, kube_context=config.kube_context, env={})


def test_the_cluster_namespaces_of_enabled_stages_are_not_removals(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True, prod=True))
    tools = _toolkit(_stage_names_runner('["dev", "prod"]', labelled_namespaces="olf-system\nolf-dev\nolf-prod\n"))

    platform.require_no_stage_removal(config, tools, kube_context=config.kube_context, env={})


def test_a_stage_named_by_both_signals_is_reported_once(tmp_path: Path) -> None:
    config = _config(tmp_path, topology=_topology(dev=True))
    tools = _toolkit(_stage_names_runner('["dev", "prod"]', labelled_namespaces="olf-system\nolf-dev\nolf-prod\n"))

    assert platform.deployed_stages_the_topology_dropped(
        config, tools, kube_context=config.kube_context, env={}
    ) == ("prod",)
