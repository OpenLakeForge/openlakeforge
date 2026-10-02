"""Floe and Helm checks: parsed rendered profiles, contracts, and values files."""

from __future__ import annotations

from pathlib import Path

import yaml

from olf import floe as floe_module
from olf.contracts_check._report import CheckResult

_AWS_GLUE_ENV_OVERRIDE = {
    "OPENLAKEFORGE_STORAGE_IMPLEMENTATION": "storage.aws_s3",
    "OPENLAKEFORGE_CATALOG_TYPE": "glue",
    "OPENLAKEFORGE_CATALOG_PROVIDER": "aws-glue",
    "OPENLAKEFORGE_CATALOG_GLUE_DATABASE": "sales_silver",
}


def _check_floe_rendered_profile() -> CheckResult:
    """Render the Floe profile in-process (no subprocess, no `olf floe
    render-profile` indirection) and assert on the *parsed* YAML, both for
    the default (local/REST) branch and the AWS-Glue-shaped env override."""
    name = "floe_rendered_profile"
    errors: list[str] = []

    local_parsed = yaml.safe_load(floe_module.render_profile({}))
    if local_parsed.get("catalogs", {}).get("default") != "iceberg_catalog":
        errors.append("local profile: catalogs.default must be 'iceberg_catalog'")
    local_definitions = local_parsed.get("catalogs", {}).get("definitions", [])
    if not local_definitions or local_definitions[0].get("type") != "rest":
        errors.append("local profile: catalogs.definitions[0].type must be 'rest'")

    aws_parsed = yaml.safe_load(floe_module.render_profile(_AWS_GLUE_ENV_OVERRIDE))
    aws_definitions = aws_parsed.get("catalogs", {}).get("definitions", [])
    if not aws_definitions or aws_definitions[0].get("type") != "glue":
        errors.append("aws-glue profile: catalogs.definitions[0].type must be 'glue'")
    if aws_definitions and "create_database_if_missing" not in aws_definitions[0]:
        errors.append("aws-glue profile: catalog definition must set create_database_if_missing")

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail="local (rest) and aws-glue profiles rendered and parsed")


def _check_floe_contract_structure(repo_root: Path) -> CheckResult:
    """Every `lakehouse_code/silver/*/contracts/floe/*.yml` must parse as YAML
    and use provider-neutral storage aliases and a domain-scoped Silver
    namespace matching `<domain>_silver` (never a shared "silver"/"gold"
    namespace, per ADR 0002)."""
    name = "floe_contract_structure"
    errors: list[str] = []
    contract_paths = sorted(repo_root.glob("lakehouse_code/silver/*/contracts/floe/*.yml"))
    if not contract_paths:
        return CheckResult(name, ok=False, detail="no lakehouse_code/silver/*/contracts/floe/*.yml files found")

    for contract_path in contract_paths:
        rel_path = contract_path.relative_to(repo_root)
        domain = contract_path.parents[2].name
        expected_namespace = f"{domain}_silver"
        document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        storage_names = {d.get("name") for d in document.get("storages", {}).get("definitions", [])}
        for required_alias in ("lakehouse_bronze", "lakehouse_silver"):
            if required_alias not in storage_names:
                errors.append(f"{rel_path}: storages.definitions must include {required_alias!r}")
        for entity in document.get("entities", []):
            sink_iceberg = entity.get("sink", {}).get("accepted", {}).get("iceberg", {})
            namespace = sink_iceberg.get("namespace")
            if sink_iceberg.get("catalog") == "polaris":
                errors.append(
                    f"{rel_path}: entity {entity.get('name')!r} sink.accepted.iceberg.catalog must not "
                    "name the 'polaris' provider directly (use the logical 'iceberg_catalog' alias)"
                )
            if namespace != expected_namespace:
                errors.append(
                    f"{rel_path}: entity {entity.get('name')!r} sink.accepted.iceberg.namespace "
                    f"must be domain-scoped {expected_namespace!r}, got {namespace!r}"
                )

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail=f"{len(contract_paths)} Floe contract file(s) validated")


def _check_floe_profile_templates(repo_root: Path) -> CheckResult:
    """Checked-in Floe runtime profile templates must parse as YAML and
    declare the top-level shape the Floe runner expects."""
    name = "floe_profile_templates"
    errors: list[str] = []
    required_keys = ("apiVersion", "kind", "metadata", "variables", "catalogs", "execution", "validation")
    profile_paths = sorted(repo_root.glob("libs/floe/profiles/*.yml"))
    if not profile_paths:
        return CheckResult(name, ok=False, detail="no libs/floe/profiles/*.yml files found")

    for profile_path in profile_paths:
        rel_path = profile_path.relative_to(repo_root)
        document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        for required_key in required_keys:
            if required_key not in document:
                errors.append(f"{rel_path}: missing required top-level key {required_key!r}")

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail=f"{len(profile_paths)} Floe profile template(s) validated")


def _check_helm_values_as_data(repo_root: Path) -> CheckResult:
    """Parse the static Dagster Helm values input as data. Does not invoke
    `helm template` -- that remains `check-infra.sh`'s job."""
    name = "helm_values_as_data"
    values_path = repo_root / "infra/helm/values/local/dagster.yaml"
    if not values_path.is_file():
        return CheckResult(name, ok=False, detail=f"missing {values_path}")

    document = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    compute_log_manager = document.get("computeLogManager", {})
    errors: list[str] = []
    if compute_log_manager.get("type") != "S3ComputeLogManager":
        errors.append("computeLogManager.type must be 'S3ComputeLogManager'")
    s3_config = compute_log_manager.get("config", {}).get("s3ComputeLogManager", {})
    if not s3_config.get("bucket"):
        errors.append("computeLogManager.config.s3ComputeLogManager.bucket must be set")

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail="infra/helm/values/local/dagster.yaml validated as data")
