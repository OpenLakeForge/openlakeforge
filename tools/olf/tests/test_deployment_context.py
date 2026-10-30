from __future__ import annotations

import os
from pathlib import Path

import pytest

from olf.common.deployment.context import DeploymentContext, Profile
from olf.common.profile import DeploymentProfile, Preset, Provider, ProviderSpec, StageName, StageSpec, resolve_topology


def test_local_defaults(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path)

    assert ctx.provider == Provider.LOCAL
    assert ctx.namespace == "olf-dev"
    assert ctx.shared_namespace == "olf-system"
    assert ctx.kube_context == "kind-openlakeforge-local"
    assert ctx.paths.kubeconfig_path == tmp_path / ".tmp" / "kubeconfigs" / "local.yaml"
    assert ctx.paths.platform_terraform_dir == tmp_path / "infra" / "terraform" / "environments" / "local"
    assert ctx.paths.foundation_terraform_dir == tmp_path / "infra" / "terraform" / "foundations" / "local-kind"
    assert ctx.paths.docker_config_dir == tmp_path / ".tmp" / "docker" / "local"
    assert ctx.paths.helm_repository_config == tmp_path / ".tmp" / "helm" / "local" / "repositories.yaml"
    assert ctx.paths.helm_repository_cache == tmp_path / ".tmp" / "helm" / "local" / "repository-cache"


def test_local_kubeconfig_path_override(tmp_path: Path) -> None:
    override = tmp_path / "custom" / "kind-smoke.yaml"

    ctx = DeploymentContext.local(repo_root=tmp_path, kubeconfig_path=override)

    assert ctx.paths.kubeconfig_path == override
    # Only the kubeconfig path changes; every other derived path is untouched.
    assert ctx.paths.docker_config_dir == tmp_path / ".tmp" / "docker" / "local"
    assert ctx.paths.helm_repository_config == tmp_path / ".tmp" / "helm" / "local" / "repositories.yaml"


def test_local_kubeconfig_path_override_is_resolved_to_an_absolute_path(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path, kubeconfig_path=Path("relative/kubeconfig.yaml"))

    assert ctx.paths.kubeconfig_path.is_absolute()


def test_foundation_state_path_defaults_under_the_foundation_terraform_dir(tmp_path: Path) -> None:
    ctx = DeploymentContext.aws(repo_root=tmp_path)

    assert ctx.paths.foundation_state_path == ctx.paths.foundation_terraform_dir / "terraform.tfstate"


def test_foundation_state_path_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every removed provider's stack scripts honored FOUNDATION_STATE_PATH,
    used both for the state-missing precondition check and as the
    `foundation_state_path` Terraform variable supplied to the platform
    root - a non-default foundation state must not be rejected.
    """
    override = tmp_path / "custom" / "foundation.tfstate"
    monkeypatch.setenv("FOUNDATION_STATE_PATH", str(override))

    ctx = DeploymentContext.aws(repo_root=tmp_path)

    assert ctx.paths.foundation_state_path == override


def test_foundation_state_path_override_applies_to_every_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom" / "foundation.tfstate"
    monkeypatch.setenv("FOUNDATION_STATE_PATH", str(override))

    assert DeploymentContext.local(repo_root=tmp_path).paths.foundation_state_path == override
    assert DeploymentContext.aws(repo_root=tmp_path).paths.foundation_state_path == override
    assert DeploymentContext.azure(repo_root=tmp_path).paths.foundation_state_path == override


def test_aws_kubeconfig_path_override(tmp_path: Path) -> None:
    """A kubeconfig override isolates a
    parallel `olf deploy --provider aws --kubeconfig-path "$AWS_KUBECONFIG_PATH"` run with its own kubeconfig.
    """
    override = tmp_path / "custom" / "aws-smoke.yaml"

    ctx = DeploymentContext.aws(repo_root=tmp_path, kubeconfig_path=override)

    assert ctx.paths.kubeconfig_path == override
    assert ctx.paths.docker_config_dir == tmp_path / ".tmp" / "docker" / "aws"


def test_azure_kubeconfig_path_override(tmp_path: Path) -> None:
    override = tmp_path / "custom" / "azure-smoke.yaml"

    ctx = DeploymentContext.azure(repo_root=tmp_path, kubeconfig_path=override)

    assert ctx.paths.kubeconfig_path == override
    assert ctx.paths.docker_config_dir == tmp_path / ".tmp" / "docker" / "azure"


def test_aws_and_azure_produce_independent_scoped_paths(tmp_path: Path) -> None:
    aws_ctx = DeploymentContext.aws(repo_root=tmp_path)
    azure_ctx = DeploymentContext.azure(repo_root=tmp_path)

    assert aws_ctx.paths.kubeconfig_path != azure_ctx.paths.kubeconfig_path
    assert aws_ctx.paths.docker_config_dir != azure_ctx.paths.docker_config_dir
    assert aws_ctx.paths.helm_repository_config != azure_ctx.paths.helm_repository_config
    assert aws_ctx.paths.platform_terraform_dir == tmp_path / "infra" / "terraform" / "environments" / "aws-poc"
    assert azure_ctx.paths.platform_terraform_dir == tmp_path / "infra" / "terraform" / "environments" / "azure-poc"


def test_for_provider_dispatches_to_matching_factory(tmp_path: Path) -> None:
    ctx = DeploymentContext.for_provider("aws", repo_root=tmp_path)
    assert ctx.provider == Provider.AWS

    ctx = DeploymentContext.for_provider(Provider.AZURE, repo_root=tmp_path)
    assert ctx.provider == Provider.AZURE


def test_slim_profile_disables_optional_layers(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path, profile=Profile.SLIM)

    assert ctx.features.governance_enabled is False
    assert ctx.features.analytics_enabled is False


def test_full_profile_enables_optional_layers(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path, profile=Profile.FULL)

    assert ctx.features.governance_enabled is True
    assert ctx.features.analytics_enabled is True


def test_repo_root_and_distribution_root_can_differ(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    distribution_root = tmp_path / "distribution"
    repo_root.mkdir()
    distribution_root.mkdir()

    ctx = DeploymentContext.local(repo_root=repo_root, distribution_root=distribution_root)

    assert ctx.paths.repo_root == repo_root.resolve()
    assert ctx.paths.distribution_root == distribution_root.resolve()
    assert ctx.paths.repo_root != ctx.paths.distribution_root


def test_distribution_root_defaults_to_repo_root(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path)

    assert ctx.paths.distribution_root == ctx.paths.repo_root


def test_command_env_sets_expected_keys_without_mutating_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KUBECONFIG", raising=False)
    ctx = DeploymentContext.local(repo_root=tmp_path)
    assert "KUBECONFIG" not in os.environ

    env = ctx.command_env(docker_host="unix:///var/run/docker.sock")

    assert env["KUBECONFIG"] == str(ctx.paths.kubeconfig_path)
    assert env["DOCKER_CONFIG"] == str(ctx.paths.docker_config_dir)
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert env["DOCKER_BUILDKIT"] == "1"
    assert env["BUILDKIT_PROGRESS"] == "plain"
    assert env["HELM_REPOSITORY_CONFIG"] == str(ctx.paths.helm_repository_config)
    assert env["HELM_REPOSITORY_CACHE"] == str(ctx.paths.helm_repository_cache)
    assert env["OPENLAKEFORGE_PROJECT_ROOT"] == str(ctx.paths.project.root)
    assert env["OPENLAKEFORGE_REPO_ROOT"] == str(ctx.paths.project.root)
    assert env["OLF_DISTRIBUTION_ROOT"] == str(ctx.paths.project.distribution_root)
    assert "KUBECONFIG" not in os.environ


def test_installed_context_keeps_terraform_state_and_data_outside_distribution(tmp_path: Path) -> None:
    distribution = tmp_path / "distribution"
    project = tmp_path / "project"
    state = tmp_path / "state"
    work = tmp_path / "work"
    ctx = DeploymentContext.local(
        repo_root=project,
        distribution_root=distribution,
        state_root=state,
        work_root=work,
    )

    env = ctx.command_env()

    assert ctx.paths.platform_terraform_dir == distribution / "infra/terraform/environments/local"
    assert ctx.paths.foundation_state_path == state / "local/foundation.tfstate"
    assert ctx.paths.platform_state_path == state / "local/platform.tfstate"
    assert env["OPENLAKEFORGE_TERRAFORM_DATA_ROOT"] == str(work / "local/terraform-data")
    assert env["OPENLAKEFORGE_TERRAFORM_STATE_ROOT"] == str(state / "local")
    assert env["OPENLAKEFORGE_TERRAFORM_READONLY_LOCKFILE"] == "true"


def test_installed_context_without_project_root_still_externalizes_terraform_state(tmp_path: Path) -> None:
    """The documented quick start - `uv tool install openlakeforge` then
    `olf deploy` with no `--project-root` - deploys the bundled demo, so
    `project_root` and `distribution_root` are the *same* read-only payload.

    Regression test: `command_env` used to gate the external state/data
    roots on `distribution_root != repo_root`, which is False here, so
    Terraform ran without `TF_DATA_DIR`/`-state=` and `terraform apply`
    died with "open terraform.tfstate: permission denied" against the 0555
    payload - the very first apply of the quick start.
    """
    payload = tmp_path / "payload"
    state = tmp_path / "state"
    work = tmp_path / "work"
    ctx = DeploymentContext.local(
        repo_root=payload,
        distribution_root=payload,
        state_root=state,
        work_root=work,
        cache_root=tmp_path / "cache",
    )

    env = ctx.command_env()

    assert ctx.paths.installed is True
    assert ctx.paths.repo_root == ctx.paths.distribution_root
    assert env["OPENLAKEFORGE_TERRAFORM_DATA_ROOT"] == str(work / "local/terraform-data")
    assert env["OPENLAKEFORGE_TERRAFORM_STATE_ROOT"] == str(state / "local")
    assert env["OPENLAKEFORGE_TERRAFORM_READONLY_LOCKFILE"] == "true"


def test_source_context_leaves_terraform_state_beside_the_checkout(tmp_path: Path) -> None:
    """A source checkout keeps Terraform's own directory-relative defaults -
    no `-state=`/`TF_DATA_DIR` overlay, and no read-only lockfile."""
    ctx = DeploymentContext.local(repo_root=tmp_path)

    env = ctx.command_env()

    assert ctx.paths.installed is False
    assert "OPENLAKEFORGE_TERRAFORM_DATA_ROOT" not in env
    assert "OPENLAKEFORGE_TERRAFORM_STATE_ROOT" not in env
    assert "OPENLAKEFORGE_TERRAFORM_READONLY_LOCKFILE" not in env


def test_command_env_without_docker_host_omits_it(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path)

    env = ctx.command_env()

    assert "DOCKER_HOST" not in env


def test_command_env_does_not_mutate_supplied_base_mapping(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path)
    base = {"PATH": "/usr/bin", "DOCKER_BUILDKIT": "0"}
    base_copy = dict(base)

    env = ctx.command_env(base=base)

    assert base == base_copy
    assert env["DOCKER_BUILDKIT"] == "0"  # base overrides default when caller pre-sets it
    assert env["PATH"] == "/usr/bin"


def test_aws_command_env_uses_the_profile_region(tmp_path: Path) -> None:
    topology = resolve_topology(
        DeploymentProfile(
            name="acceptance",
            provider=ProviderSpec(type=Provider.AWS, region="eu-west-3"),
            preset=Preset.SLIM,
            stages=(StageSpec(name=StageName.DEV),),
        )
    )

    env = DeploymentContext.aws(repo_root=tmp_path, topology=topology).command_env(
        base={"AWS_REGION": "eu-west-1"}
    )

    assert env["AWS_REGION"] == "eu-west-3"
    assert env["AWS_DEFAULT_REGION"] == "eu-west-3"


def test_prepare_directories_creates_owned_paths(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path)

    ctx.prepare_directories(docker_cli_plugins_source=tmp_path / "no-such-docker-home")

    assert ctx.paths.kubeconfig_path.parent.is_dir()
    assert ctx.paths.docker_config_dir.is_dir()
    assert ctx.paths.helm_repository_config.parent.is_dir()
    assert ctx.paths.helm_repository_cache.is_dir()
    assert ctx.paths.helm_cache_dir.is_dir()
    assert ctx.paths.superset_report_work_dir.is_dir()


def test_prepare_directories_links_docker_cli_plugins_when_source_exists(tmp_path: Path) -> None:
    docker_home = tmp_path / "docker-home"
    plugins_source = docker_home / "cli-plugins"
    plugins_source.mkdir(parents=True)
    (plugins_source / "buildx").write_text("fake plugin")

    ctx = DeploymentContext.local(repo_root=tmp_path)
    ctx.prepare_directories(docker_cli_plugins_source=docker_home)

    destination = ctx.paths.docker_config_dir / "cli-plugins"
    assert destination.is_symlink()
    assert destination.resolve() == plugins_source.resolve()


def test_prepare_directories_does_not_overwrite_existing_cli_plugins_link(tmp_path: Path) -> None:
    docker_home = tmp_path / "docker-home"
    plugins_source = docker_home / "cli-plugins"
    plugins_source.mkdir(parents=True)

    ctx = DeploymentContext.local(repo_root=tmp_path)
    ctx.paths.docker_config_dir.mkdir(parents=True)
    existing_destination = ctx.paths.docker_config_dir / "cli-plugins"
    existing_destination.mkdir()
    marker = existing_destination / "already-here"
    marker.write_text("do not touch")

    ctx.prepare_directories(docker_cli_plugins_source=docker_home)

    assert marker.exists()
    assert not existing_destination.is_symlink()


def test_prepare_directories_no_failure_when_source_missing(tmp_path: Path) -> None:
    ctx = DeploymentContext.local(repo_root=tmp_path)

    ctx.prepare_directories(docker_cli_plugins_source=tmp_path / "does-not-exist")

    assert not (ctx.paths.docker_config_dir / "cli-plugins").exists()
