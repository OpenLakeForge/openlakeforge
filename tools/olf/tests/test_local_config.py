from __future__ import annotations

from pathlib import Path

from olf.deployment.context import DeploymentContext, Profile
from olf.deployment.local.config import LocalDeploymentConfig


def _write_full_catalog(repo_root: Path, *, overrides: dict[str, str] | None = None) -> Path:
    """A catalog covering every chart the local provider might resolve - a
    real `component-catalog.yaml` always declares all of them, so a partial
    fixture would make `CatalogChart.load` raise for any chart beyond the
    one(s) a given test focuses on."""
    from olf.deployment.charts import CHART_DEFAULTS

    overrides = overrides or {}
    lines = ["components:", "  helm:", "    charts:"]
    for index, (name, default) in enumerate(CHART_DEFAULTS.items()):
        sha256 = overrides.get(name, f"{index:0>2}" * 32)
        lines.extend(
            [
                f"      {name}:",
                f"        repository: {default.repository}",
                f"        reference: {default.chart_ref}",
                f"        version: {default.version}",
                f'        sha256: "{sha256}"',
            ]
        )
    catalog_path = repo_root / "release/component-catalog.yaml"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text("\n".join(lines) + "\n")
    return catalog_path


def test_full_profile_defaults(tmp_path: Path) -> None:
    context = DeploymentContext.local(repo_root=tmp_path, profile=Profile.FULL)

    config = LocalDeploymentConfig.from_environment({}, context=context)

    assert config.cluster.name == "openlakeforge-local"
    assert config.cluster.config_path == tmp_path / "infra/kind/local/kind-cluster.yaml"
    assert config.cluster.wait_timeout == "120s"
    assert config.cluster.reset_existing is False
    assert config.images.project_code_image == "ghcr.io/openlakeforge/project-code:local"
    assert config.images.superset_image == "ghcr.io/openlakeforge/superset:local"
    assert config.images.project_code_pull_policy == "Never"
    assert config.charts["trino"].version == "1.42.2"
    assert config.charts["trino"].package_path == context.paths.helm_cache_dir / "trino-1.42.2.tgz"
    assert set(config.charts.settings) == {
        "trino",
        "dagster",
        "seaweedfs",
        "polaris",
        "openmetadata",
        "openmetadata-dependencies",
        "superset",
        "traefik",
        "cert-manager",
    }
    assert config.terraform.var_file is None
    assert config.terraform.apply_retry.max_attempts == 4
    assert config.terraform.apply_retry.delay_seconds == 20.0
    assert config.prefetch.pull_retry.max_attempts == 5
    assert config.prefetch.pull_retry.delay_seconds == 20.0
    assert config.floe.version == "0.6.11"
    assert config.floe.image == "ghcr.io/malon64/floe:0.6.11"
    assert config.floe.runtime_artifact_dir == tmp_path / ".tmp/floe-runtime/local"
    assert config.force_foundation_down is False
    assert config.features.governance_enabled is True
    assert config.features.analytics_enabled is True


def test_the_slim_preset_needs_no_var_file(tmp_path: Path) -> None:
    """Capabilities travel in the resolved topology, not in a platform-owned
    tfvars file: a slim run passes no var file at all."""
    context = DeploymentContext.local(repo_root=tmp_path, profile=Profile.SLIM)

    config = LocalDeploymentConfig.from_environment({}, context=context)

    assert config.terraform.var_file is None
    assert config.features.governance_enabled is False
    assert config.features.analytics_enabled is False


def test_an_explicit_var_file_is_resolved_against_the_project(tmp_path: Path) -> None:
    context = DeploymentContext.local(repo_root=tmp_path, profile=Profile.SLIM)

    config = LocalDeploymentConfig.from_environment({}, context=context, var_file=Path("custom.tfvars"))

    assert config.terraform.var_file == tmp_path / "custom.tfvars"


def test_environment_overrides_are_honored(tmp_path: Path) -> None:
    context = DeploymentContext.local(repo_root=tmp_path)
    environ = {
        "PROJECT_CODE_IMAGE_REPOSITORY": "example.com/project-code",
        "PROJECT_CODE_IMAGE_TAG": "v1",
        "LOCAL_FOUNDATION_RESET": "true",
        "LOCAL_FOUNDATION_FORCE_DOWN": "true",
        "LOCAL_UP_RETRY_ATTEMPTS": "2",
        "LOCAL_UP_RETRY_DELAY_SECONDS": "5",
        "DOCKER_PULL_ATTEMPTS": "9",
    }

    config = LocalDeploymentConfig.from_environment(environ, context=context)

    assert config.images.project_code_image == "example.com/project-code:v1"
    assert config.cluster.reset_existing is True
    assert config.force_foundation_down is True
    assert config.terraform.apply_retry.max_attempts == 2
    assert config.terraform.apply_retry.delay_seconds == 5.0
    assert config.images.pull_retry.max_attempts == 9


def test_docker_retry_fallback_chain(tmp_path: Path) -> None:
    context = DeploymentContext.local(repo_root=tmp_path)

    default_config = LocalDeploymentConfig.from_environment({}, context=context)
    assert default_config.images.pull_retry.max_attempts == 3
    assert default_config.images.pull_retry.delay_seconds == 10.0

    generic_config = LocalDeploymentConfig.from_environment(
        {"DOCKER_REGISTRY_ATTEMPTS": "7", "DOCKER_REGISTRY_RETRY_DELAY_SECONDS": "3"}, context=context
    )
    assert generic_config.images.pull_retry.max_attempts == 7
    assert generic_config.images.pull_retry.delay_seconds == 3.0

    specific_config = LocalDeploymentConfig.from_environment(
        {"DOCKER_REGISTRY_ATTEMPTS": "7", "DOCKER_PULL_ATTEMPTS": "11"}, context=context
    )
    assert specific_config.images.pull_retry.max_attempts == 11


def test_installed_default_project_still_pins_the_catalog_chart_digest(tmp_path: Path) -> None:
    """Companion to the `command_env` regression: the quick start's
    `project_root == distribution_root` (bundled demo) must not read as
    "source mode". It previously did, so the installed CLI accepted whatever
    Trino chart Helm downloaded instead of verifying the catalog digest.
    """
    payload = tmp_path / "payload"
    digest = "a" * 64
    _write_full_catalog(payload, overrides={"trino": digest})
    context = DeploymentContext.local(
        repo_root=payload,
        distribution_root=payload,
        state_root=tmp_path / "state",
        work_root=tmp_path / "work",
        cache_root=tmp_path / "cache",
    )

    config = LocalDeploymentConfig.from_environment({}, context=context)

    assert config.charts["trino"].sha256 == digest
    assert config.charts["trino"].package_path == tmp_path / "cache/helm" / f"{digest}.tgz"


def test_source_checkout_does_not_pin_chart_digests(tmp_path: Path) -> None:
    _write_full_catalog(tmp_path, overrides={"trino": "a" * 64})
    context = DeploymentContext.local(repo_root=tmp_path)

    config = LocalDeploymentConfig.from_environment({}, context=context)

    assert config.charts["trino"].sha256 is None


def test_relative_local_var_file_resolves_against_the_writable_project(tmp_path: Path) -> None:
    """A relative `--var-file`/`LOCAL_TFVARS_FILE` is the user's own tfvars -
    for an installed deployment with `--project-root`, that file lives in
    the writable project, never inside the read-only distribution payload.
    Regression test: this used to resolve against distribution_root.
    """
    project = tmp_path / "project"
    distribution = tmp_path / "distribution"
    project.mkdir()
    distribution.mkdir()
    context = DeploymentContext.local(repo_root=project, distribution_root=distribution)

    config = LocalDeploymentConfig.from_environment({}, context=context, var_file=Path("custom.tfvars"))

    assert config.terraform.var_file == project / "custom.tfvars"


def test_relative_local_tfvars_file_env_var_resolves_against_the_writable_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    distribution = tmp_path / "distribution"
    project.mkdir()
    distribution.mkdir()
    context = DeploymentContext.local(repo_root=project, distribution_root=distribution)

    config = LocalDeploymentConfig.from_environment({"LOCAL_TFVARS_FILE": "custom.tfvars"}, context=context)

    assert config.terraform.var_file == project / "custom.tfvars"


def test_a_user_var_file_resolves_against_the_project_not_the_payload(tmp_path: Path) -> None:
    """A user-provided tfvars file lives in the writable project; the
    distribution payload is read-only and owns no var file of its own."""
    project = tmp_path / "project"
    distribution = tmp_path / "distribution"
    project.mkdir()
    distribution.mkdir()
    context = DeploymentContext.local(repo_root=project, distribution_root=distribution, profile=Profile.SLIM)

    config = LocalDeploymentConfig.from_environment({"LOCAL_TFVARS_FILE": "custom.tfvars"}, context=context)

    assert config.terraform.var_file == project / "custom.tfvars"
