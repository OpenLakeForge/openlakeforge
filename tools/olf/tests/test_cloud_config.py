from __future__ import annotations

import re
from pathlib import Path

from olf.deployment.cloud.config import (
    CloudChartSettings,
    CloudDeploymentConfig,
    CloudImageSettings,
    CloudTerraformSettings,
    default_image_tag,
)
from olf.deployment.context import DeploymentContext, DeploymentFeatures


def test_aws_image_settings_defaults_to_ecr_public_python_base_image() -> None:
    settings = CloudImageSettings.from_environment({}, scope="aws", image_tag="aws-abc123")

    assert settings.project_code_python_base_image.startswith("public.ecr.aws/docker/library/python:3.12-slim@")
    assert settings.project_code_dbt_profile_env == "aws"
    assert settings.project_code_tag == "aws-abc123"
    assert settings.project_code_pull_policy == "Always"
    assert settings.project_code_repository == ""
    assert settings.image_platform == "linux/amd64"


def test_azure_image_settings_defaults_to_docker_hub_python_base_image() -> None:
    settings = CloudImageSettings.from_environment({}, scope="azure", image_tag="azure-abc123")

    assert settings.project_code_python_base_image.startswith("python:3.12-slim@")
    assert not settings.project_code_python_base_image.startswith("public.ecr.aws")
    assert settings.project_code_dbt_profile_env == "azure"


def test_image_platform_env_var_is_scope_prefixed() -> None:
    aws_settings = CloudImageSettings.from_environment(
        {"AWS_IMAGE_PLATFORM": "linux/arm64"}, scope="aws", image_tag="t"
    )
    azure_settings = CloudImageSettings.from_environment(
        {"AZURE_IMAGE_PLATFORM": "linux/arm64"}, scope="azure", image_tag="t"
    )

    assert aws_settings.image_platform == "linux/arm64"
    assert azure_settings.image_platform == "linux/arm64"


def test_image_settings_repository_overrides_are_honored() -> None:
    settings = CloudImageSettings.from_environment(
        {
            "PROJECT_CODE_IMAGE_REPOSITORY": "123.dkr.ecr.eu-west-1.amazonaws.com/project-code",
            "SUPERSET_IMAGE_REPOSITORY": "123.dkr.ecr.eu-west-1.amazonaws.com/superset",
        },
        scope="aws",
        image_tag="aws-abc123",
    )

    assert settings.project_code_image == "123.dkr.ecr.eu-west-1.amazonaws.com/project-code:aws-abc123"
    assert settings.superset_image == "123.dkr.ecr.eu-west-1.amazonaws.com/superset:aws-abc123"


def test_provider_prefixed_image_aliases_are_honored_after_generic_overrides() -> None:
    for scope in ("aws", "azure"):
        aliases = {
            f"{scope.upper()}_PROJECT_CODE_IMAGE_REPOSITORY": "registry.example/project-code",
            f"{scope.upper()}_PROJECT_CODE_IMAGE_TAG": f"{scope}-custom",
            f"{scope.upper()}_SUPERSET_IMAGE_REPOSITORY": "registry.example/superset",
            f"{scope.upper()}_SUPERSET_IMAGE_TAG": f"{scope}-custom",
        }
        settings = CloudImageSettings.from_environment(aliases, scope=scope, image_tag=f"{scope}-default")
        generic_wins = CloudImageSettings.from_environment(
            {**aliases, "PROJECT_CODE_IMAGE_TAG": "generic"}, scope=scope, image_tag=f"{scope}-default"
        )

        assert settings.project_code_image == f"registry.example/project-code:{scope}-custom"
        assert settings.superset_image == f"registry.example/superset:{scope}-custom"
        assert generic_wins.project_code_tag == "generic"


_FULL_FEATURES = DeploymentFeatures(governance_enabled=True, analytics_enabled=True)


def _no_catalog_kwargs(tmp_path: Path) -> dict:
    return {
        "cache_root": tmp_path / "cache",
        "catalog_path": tmp_path / "no-such-catalog.yaml",
        "installed": False,
        "scope": "aws",
        "features": _FULL_FEATURES,
    }


def _write_catalog(tmp_path: Path, *, trino_sha256: str, dagster_sha256: str) -> Path:
    """A catalog covering every chart `CloudChartSettings` might resolve -
    a real `component-catalog.yaml` always declares all of them, so a
    partial fixture would make `CatalogChart.load` raise for any chart
    beyond the two this fixture used to focus on."""
    from olf.deployment.charts import CHART_DEFAULTS

    overrides = {"trino": trino_sha256, "dagster": dagster_sha256}
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
    catalog_path = tmp_path / "release/component-catalog.yaml"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text("\n".join(lines) + "\n")
    return catalog_path


def test_chart_settings_defaults_trino_and_dagster_package_paths(tmp_path: Path) -> None:
    helm_cache_dir = tmp_path / "helm/aws/charts"

    settings = CloudChartSettings.from_environment(
        {}, helm_cache_dir=helm_cache_dir, **_no_catalog_kwargs(tmp_path)
    )

    assert settings["trino"].package_path == helm_cache_dir / "trino-1.42.2.tgz"
    assert settings["dagster"].package_path == helm_cache_dir / "dagster-1.13.7-no-schema.tgz"
    assert settings["trino"].chart_ref == "trino/trino"
    assert settings["dagster"].chart_ref == "dagster/dagster"
    assert settings["trino"].sha256 is None
    assert settings["dagster"].sha256 is None


def test_chart_settings_scope_excludes_seaweedfs_and_polaris_for_aws(tmp_path: Path) -> None:
    settings = CloudChartSettings.from_environment(
        {}, helm_cache_dir=tmp_path / "helm/aws/charts", **_no_catalog_kwargs(tmp_path)
    )

    assert set(settings.settings) == {
        "trino",
        "dagster",
        "openmetadata",
        "openmetadata-dependencies",
        "superset",
    }


def test_chart_settings_scope_includes_seaweedfs_and_polaris_for_azure(tmp_path: Path) -> None:
    kwargs = _no_catalog_kwargs(tmp_path)
    kwargs["scope"] = "azure"
    settings = CloudChartSettings.from_environment({}, helm_cache_dir=tmp_path / "helm/azure/charts", **kwargs)

    assert set(settings.settings) == {
        "trino",
        "dagster",
        "seaweedfs",
        "polaris",
        "openmetadata",
        "openmetadata-dependencies",
        "superset",
    }


def test_chart_settings_slim_profile_excludes_governance_and_analytics_charts(tmp_path: Path) -> None:
    kwargs = _no_catalog_kwargs(tmp_path)
    kwargs["features"] = DeploymentFeatures(governance_enabled=False, analytics_enabled=False)
    settings = CloudChartSettings.from_environment({}, helm_cache_dir=tmp_path / "helm/aws/charts", **kwargs)

    assert set(settings.settings) == {"trino", "dagster"}


def test_chart_settings_honors_explicit_package_path_overrides(tmp_path: Path) -> None:
    trino_override = tmp_path / "custom-trino.tgz"
    dagster_override = tmp_path / "custom-dagster.tgz"

    settings = CloudChartSettings.from_environment(
        {
            "TRINO_CHART_PACKAGE_PATH": str(trino_override),
            "DAGSTER_CHART_PACKAGE_PATH": str(dagster_override),
        },
        helm_cache_dir=tmp_path / "helm/aws/charts",
        **_no_catalog_kwargs(tmp_path),
    )

    assert settings["trino"].package_path == trino_override
    assert settings["dagster"].package_path == dagster_override


def test_chart_settings_pins_digests_from_catalog_when_installed(tmp_path: Path) -> None:
    """Mirrors the local provider's Trino digest pinning
    (`olf.deployment.local.config.ChartSettings`): an installed
    distribution must verify both cloud charts against the component
    catalog, not accept whatever Helm downloads."""
    trino_sha256 = "a" * 64
    dagster_sha256 = "b" * 64
    catalog_path = _write_catalog(tmp_path, trino_sha256=trino_sha256, dagster_sha256=dagster_sha256)
    cache_root = tmp_path / "cache"

    settings = CloudChartSettings.from_environment(
        {},
        helm_cache_dir=tmp_path / "helm/aws/charts",
        cache_root=cache_root,
        catalog_path=catalog_path,
        installed=True,
        scope="aws",
        features=_FULL_FEATURES,
    )

    assert settings["trino"].sha256 == trino_sha256
    assert settings["dagster"].sha256 == dagster_sha256
    assert settings["trino"].package_path == cache_root / "helm" / f"{trino_sha256}.tgz"
    assert settings["dagster"].package_path == cache_root / "helm" / f"{dagster_sha256}-no-schema.tgz"
    assert settings["trino"].version == "1.42.2"
    assert settings["dagster"].version == "1.13.7"


def test_chart_settings_does_not_pin_digests_in_source_mode_even_with_a_catalog(tmp_path: Path) -> None:
    catalog_path = _write_catalog(tmp_path, trino_sha256="a" * 64, dagster_sha256="b" * 64)

    settings = CloudChartSettings.from_environment(
        {},
        helm_cache_dir=tmp_path / "helm/aws/charts",
        cache_root=tmp_path / "cache",
        catalog_path=catalog_path,
        installed=False,
        scope="aws",
        features=_FULL_FEATURES,
    )

    assert settings["trino"].sha256 is None
    assert settings["dagster"].sha256 is None


def test_aws_terraform_settings_uses_default_tfvars_only_if_it_exists(tmp_path: Path) -> None:
    platform_dir = tmp_path / "infra/terraform/environments/aws-poc"
    platform_dir.mkdir(parents=True)

    absent = CloudTerraformSettings.from_environment(
        {}, repo_root=tmp_path, platform_terraform_dir=platform_dir, scope="aws"
    )
    assert absent.var_file is None

    (platform_dir / "sandbox.tfvars").write_text('region = "eu-west-1"\n')
    present = CloudTerraformSettings.from_environment(
        {}, repo_root=tmp_path, platform_terraform_dir=platform_dir, scope="aws"
    )
    assert present.var_file == platform_dir / "sandbox.tfvars"


def test_azure_terraform_settings_has_no_default_var_file(tmp_path: Path) -> None:
    platform_dir = tmp_path / "infra/terraform/environments/azure-poc"
    platform_dir.mkdir(parents=True)
    (platform_dir / "sandbox.tfvars").write_text("resource_group = \"rg\"\n")

    settings = CloudTerraformSettings.from_environment(
        {}, repo_root=tmp_path, platform_terraform_dir=platform_dir, scope="azure"
    )

    assert settings.var_file is None


def test_terraform_settings_explicit_aws_tfvars_file_override_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.tfvars"
    explicit.write_text("x = 1\n")

    aws_settings = CloudTerraformSettings.from_environment(
        {"AWS_TFVARS_FILE": str(explicit)}, repo_root=tmp_path, platform_terraform_dir=tmp_path, scope="aws"
    )

    assert aws_settings.var_file == explicit


def test_terraform_settings_ignores_azure_tfvars_file_for_the_platform_apply(tmp_path: Path) -> None:
    """AZURE_TFVARS_FILE is a foundation-only concept (handled by AzureBackend.
    foundation_tfvars_file) - Azure's platform Terraform root declares none of the
    foundation-only variables (resource_group_name, location, node_vm_size, ...) a
    foundation tfvars file sets, so passing it through here would fail `-var-file`
    validation on every azure-up/azure-platform-up/platform teardown.
    """
    foundation_tfvars = tmp_path / "sandbox.tfvars"
    foundation_tfvars.write_text("resource_group_name = \"rg\"\n")

    azure_settings = CloudTerraformSettings.from_environment(
        {"AZURE_TFVARS_FILE": str(foundation_tfvars)},
        repo_root=tmp_path,
        platform_terraform_dir=tmp_path,
        scope="azure",
    )

    assert azure_settings.var_file is None


def test_terraform_settings_apply_retry_defaults_and_scope_prefixed_overrides(tmp_path: Path) -> None:
    default_settings = CloudTerraformSettings.from_environment(
        {}, repo_root=tmp_path, platform_terraform_dir=tmp_path, scope="aws"
    )
    assert default_settings.apply_retry.max_attempts == 4
    assert default_settings.apply_retry.delay_seconds == 20.0

    overridden = CloudTerraformSettings.from_environment(
        {"AWS_UP_RETRY_ATTEMPTS": "7", "AWS_UP_RETRY_DELAY_SECONDS": "5"},
        repo_root=tmp_path,
        platform_terraform_dir=tmp_path,
        scope="aws",
    )
    assert overridden.apply_retry.max_attempts == 7
    assert overridden.apply_retry.delay_seconds == 5.0


def test_default_image_tag_falls_back_to_utc_timestamp_outside_a_git_repo(tmp_path: Path) -> None:
    tag = default_image_tag(tmp_path, scope="aws")

    assert re.fullmatch(r"aws-\d{14}", tag), tag


def test_default_image_tag_prefers_a_stable_distribution_identity_over_a_timestamp(tmp_path: Path) -> None:
    """An installed, non-git project root (the bundled demo, or a plain
    --project-root folder) must not fall back to the wall-clock timestamp:
    separate `olf deploy --phase platform` and `--phase artifacts` are
    separate CLI invocations, so a fresh timestamp each time would leave
    platform configuring Dagster with one tag while artifacts builds and
    pushes a different one - the code server could never pull it."""
    digest = "a" * 64
    payload = tmp_path / "distributions/0.1.0-alpha.1" / digest / "payload"
    payload.mkdir(parents=True)

    first = default_image_tag(payload, scope="aws", distribution_root=payload)
    second = default_image_tag(payload, scope="aws", distribution_root=payload)

    assert first == second
    assert first == f"aws-{digest[:12]}"


def test_default_image_tag_ignores_a_distribution_root_that_is_not_content_addressed(tmp_path: Path) -> None:
    """A source checkout's distribution_root == repo_root, so its parent
    directory name is arbitrary - must not be mistaken for a payload digest."""
    tag = default_image_tag(tmp_path, scope="aws", distribution_root=tmp_path)

    assert re.fullmatch(r"aws-\d{14}", tag), tag


def test_cloud_deployment_config_from_environment_builds_full_config(tmp_path: Path) -> None:
    context = DeploymentContext.aws(repo_root=tmp_path)

    config = CloudDeploymentConfig.from_environment({"AWS_IMAGE_TAG": "aws-fixed"}, context=context)

    assert config.images.project_code_tag == "aws-fixed"
    assert config.images.superset_tag == "aws-fixed"
    assert config.namespace == context.namespace
    assert config.kube_context == context.kube_context
    assert config.force_foundation_down is False


def test_cloud_deployment_config_var_file_argument_overrides_resolved_default_for_aws(tmp_path: Path) -> None:
    context = DeploymentContext.aws(repo_root=tmp_path)
    explicit = tmp_path / "explicit.tfvars"

    config = CloudDeploymentConfig.from_environment({}, context=context, var_file=explicit)

    assert config.terraform.var_file == explicit
    assert config.terraform.foundation_var_file == explicit


def test_relative_var_file_resolves_against_the_writable_project_not_the_payload(tmp_path: Path) -> None:
    """A relative `--var-file` is the user's own account/tag tfvars - for an
    installed deployment with `--project-root`, that file lives in the
    user's writable project, never inside the read-only distribution
    payload. Regression test: this used to resolve against
    distribution_root, pointing at a file that can't exist there.
    """
    project = tmp_path / "project"
    distribution = tmp_path / "distribution"
    project.mkdir()
    distribution.mkdir()
    context = DeploymentContext.aws(repo_root=project, distribution_root=distribution)

    config = CloudDeploymentConfig.from_environment({}, context=context, var_file=Path("sandbox.tfvars"))

    assert config.terraform.var_file == project / "sandbox.tfvars"
    assert config.terraform.foundation_var_file == project / "sandbox.tfvars"


def test_relative_aws_tfvars_file_env_var_resolves_against_the_writable_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    distribution = tmp_path / "distribution"
    project.mkdir()
    distribution.mkdir()
    context = DeploymentContext.aws(repo_root=project, distribution_root=distribution)

    config = CloudDeploymentConfig.from_environment({"AWS_TFVARS_FILE": "sandbox.tfvars"}, context=context)

    assert config.terraform.var_file == project / "sandbox.tfvars"


def test_cloud_deployment_config_var_file_argument_resolves_relative_paths_against_repo_root(
    tmp_path: Path,
) -> None:
    """P2 regression: Terraform runs with `-chdir=<foundation-or-platform-
    root>`, so a relative `--var-file` (as the CLI hands it through
    unresolved) must be normalized against the repo root before storage -
    otherwise Terraform resolves it beneath whichever Terraform root
    happens to run, not beneath the repo (and AWS reuses the override
    across two different roots in the same `deploy` run).
    """
    context = DeploymentContext.aws(repo_root=tmp_path)
    relative = Path("configs/cloud.tfvars")

    config = CloudDeploymentConfig.from_environment({}, context=context, var_file=relative)

    assert config.terraform.var_file == tmp_path / relative
    assert config.terraform.foundation_var_file == tmp_path / relative


def test_cloud_deployment_config_var_file_argument_never_reaches_azure_platform_apply(tmp_path: Path) -> None:
    """An explicit `--var-file` on Azure must route only through
    `foundation_var_file`, never `var_file` - `var_file` also feeds the
    platform apply (`cloud/platform.py`), and Azure's platform Terraform
    root rejects a tfvars file entirely (ADR 0008, binding requirement).
    A combined `olf deploy --provider azure --var-file <foundation.tfvars>`
    run must not fail Terraform `-var-file` validation at the platform
    phase.
    """
    context = DeploymentContext.azure(repo_root=tmp_path)
    explicit = tmp_path / "explicit.tfvars"

    config = CloudDeploymentConfig.from_environment({}, context=context, var_file=explicit)

    assert config.terraform.var_file is None
    assert config.terraform.foundation_var_file == explicit
