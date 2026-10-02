"""Terraform HCL checks: parsed `contracts.tf`/`main.tf` and applied provider contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import hcl2

from olf import contracts as contracts_module
from olf.contracts_check._report import CheckResult

# Terraform environment roots that carry a `contracts.tf` provider-contract
# surface (ADR-defined; local/azure-poc/aws-poc are the only ones today).
_ENVIRONMENT_ROOTS = ("local", "azure-poc", "aws-poc")

# Every `provider_contracts` capability local that `contracts.tf` must
# declare in each environment (order matches `provider_contracts`' keys).
_REQUIRED_CONTRACT_LOCALS = (
    "foundation_contract",
    "kubernetes_platform_contract",
    "storage_contract",
    "metadata_database_contract",
    "catalog_contract",
    "query_contract",
    "orchestration_contract",
    "governance_contract",
    "reporting_contract",
    "artifact_registry_contract",
    "artifact_bucket_contract",
    "artifact_contract",
    "secrets_contract",
    "identity_contract",
    "access_contract",
    "observability_contract",
    "provider_contracts",
)

# Stage-scoped contract locals. Required of every root: all three now index
# their per-stage service instances -- Dagster included -- off these.
_REQUIRED_STAGE_CONTRACT_LOCALS = (
    "stage_metadata_database_contracts",
    "selected_stage_analytics",
)

# Stage-topology locals every root derives in `main.tf` rather than in its
# contract surface.
_REQUIRED_TOPOLOGY_LOCALS = (
    "enabled_stages",
    "analytics_stages",
    "governance_enabled",
    "stage_namespaces",
    "stage_service_accounts",
    "stage_databases",
    "selected_stage",
)

# Cross-field invariants each environment's `contracts.tf` must declare as a
# native Terraform `check` block (ADR-defined; these only evaluate under
# `terraform plan`/`apply`, so this tier only confirms they are declared).
#
# The stage-isolation invariants are required of every root: without a
# namespace per stage, its service instances pinned to it, and a metadata
# database nobody else holds, two stages' Dagster run history and schedule
# state mix (#134).
_REQUIRED_STAGE_CONTRACT_CHECKS = (
    "stage_namespaces_are_distinct",
    "stage_services_stay_in_their_own_stage",
    "stage_metadata_state_is_not_shared",
)

# The "adapters are explicit" check is named per-provider; the OpenMetadata
# FQN check only applies where a Polaris-backed catalog names a database.
_REQUIRED_CONTRACT_CHECKS_BY_ENV = {
    "local": (
        "foundation_contract_matches_platform_context",
        "local_contract_adapters_are_explicit",
        "catalog_contract_consumer_support",
        "openmetadata_catalog_fqn_uses_lakehouse_database",
    ),
    "azure-poc": (
        "foundation_contract_matches_platform_context",
        "azure_contract_adapters_are_explicit",
        "catalog_contract_consumer_support",
        "openmetadata_catalog_fqn_uses_lakehouse_database",
    ),
    "aws-poc": (
        "foundation_contract_matches_platform_context",
        "aws_contract_adapters_are_explicit",
        "catalog_contract_consumer_support",
    ),
}

# ADR 0002: catalog namespace/database lifecycle belongs to Phase 2
# (`olf catalog sync-namespaces`), never to Terraform-owned locals or module
# arguments in `main.tf`/`contracts.tf`.
_FORBIDDEN_PHASE_TWO_FIELDS = (
    "catalog_namespaces",
    "silver_namespaces",
    "gold_namespaces",
    "silver_schema_fqns",
    "gold_schema_fqns",
)


def _parse_hcl(path: Path) -> dict[str, Any]:
    return hcl2.loads(path.read_text(encoding="utf-8"))


def _merged_locals(document: dict[str, Any]) -> dict[str, Any]:
    """Flatten every `locals { }` block's name -> value map. Values built
    from `merge(module.X.contract, {...})` collapse to an opaque
    '${merge(...)}' expression string -- hcl2 parses HCL syntax, not
    Terraform semantics, so it never resolves module references or function
    calls. Callers that need those scope their check to that string."""
    merged: dict[str, Any] = {}
    for block in document.get("locals", []):
        merged.update(block)
    return merged


def _check_hcl_structured_contracts(repo_root: Path) -> CheckResult:
    """Tier 1+2: parse each environment's `contracts.tf` and assert on the
    parsed tree -- required locals exist, required `check` blocks are
    declared, and Phase-2-forbidden fields are absent from the named local's
    own (possibly opaque) expression text. This is authoring-time guarding:
    nobody should write a forbidden key at all, regardless of whether it
    would ever evaluate truthy (see `_check_hcl_phase_two_invariants` for the
    tier that needs actually-resolved values)."""
    name = "hcl_structured_contracts"
    errors: list[str] = []
    environments_checked = 0

    for env in _ENVIRONMENT_ROOTS:
        contracts_path = repo_root / "infra/terraform/environments" / env / "contracts.tf"
        if not contracts_path.is_file():
            errors.append(f"{env}: missing contracts.tf at {contracts_path}")
            continue
        environments_checked += 1
        document = _parse_hcl(contracts_path)
        locals_map = _merged_locals(document)

        for required_local in (*_REQUIRED_CONTRACT_LOCALS, *_REQUIRED_STAGE_CONTRACT_LOCALS):
            if required_local not in locals_map:
                errors.append(f"{env}/contracts.tf: missing required local {required_local!r}")

        declared_checks = {key for block in document.get("check", []) for key in block}
        for required_check in (*_REQUIRED_STAGE_CONTRACT_CHECKS, *_REQUIRED_CONTRACT_CHECKS_BY_ENV.get(env, ())):
            if required_check not in declared_checks:
                errors.append(f"{env}/contracts.tf: missing required check block {required_check!r}")

        for local_name, value in locals_map.items():
            if not isinstance(value, str):
                continue
            for forbidden_field in _FORBIDDEN_PHASE_TWO_FIELDS:
                if f'"{forbidden_field}"' in value or f"{forbidden_field} " in value:
                    errors.append(
                        f"{env}/contracts.tf: local {local_name!r} references Phase-2-owned field "
                        f"{forbidden_field!r} (ADR 0002: namespaces/schemas are reconciled by "
                        f"`olf catalog sync-namespaces`, not declared in Terraform)"
                    )

        main_path = repo_root / "infra/terraform/environments" / env / "main.tf"
        if not main_path.is_file():
            errors.append(f"{env}: missing main.tf at {main_path}")
            continue
        main_document = _parse_hcl(main_path)
        main_locals = _merged_locals(main_document)
        if main_locals.get("catalog_namespace_model") != "medallion-owner":
            errors.append(f"{env}/main.tf: local.catalog_namespace_model must be 'medallion-owner'")
        for required_local in _REQUIRED_TOPOLOGY_LOCALS:
            if required_local not in main_locals:
                errors.append(f"{env}/main.tf: missing required topology local {required_local!r}")
        for forbidden_field in _FORBIDDEN_PHASE_TWO_FIELDS:
            if forbidden_field in main_locals:
                errors.append(f"{env}/main.tf: locals must not declare Phase-2-owned field {forbidden_field!r}")
        for module_block in main_document.get("module", []):
            for module_name, module_body in module_block.items():
                if not isinstance(module_body, dict):
                    continue
                for forbidden_field in _FORBIDDEN_PHASE_TWO_FIELDS:
                    if forbidden_field in module_body:
                        errors.append(
                            f"{env}/main.tf: module {module_name!r} must not receive Phase-2-owned "
                            f"argument {forbidden_field!r}"
                        )

    glue_main_path = repo_root / "infra/terraform/modules/catalog/aws-glue/main.tf"
    if glue_main_path.is_file():
        glue_document = _parse_hcl(glue_main_path)
        glue_resources = {
            f"{resource_type}.{resource_name}"
            for resource_block in glue_document.get("resource", [])
            for resource_type, instances in resource_block.items()
            for resource_name in instances
        }
        if "aws_glue_catalog_database" in {rtype for rtype, _ in (r.split(".", 1) for r in glue_resources)}:
            errors.append(
                "infra/terraform/modules/catalog/aws-glue/main.tf: must not create aws_glue_catalog_database "
                "resources (ADR 0002: Phase 2 owns database lifecycle)"
            )
        removed_blocks = glue_document.get("removed", [])
        has_namespace_removal = any(
            block.get("from") == "${aws_glue_catalog_database.namespace}"
            and any(lc.get("destroy") is False for lc in block.get("lifecycle", []))
            for block in removed_blocks
        )
        if not has_namespace_removal:
            errors.append(
                "infra/terraform/modules/catalog/aws-glue/main.tf: missing a `removed { from = "
                "aws_glue_catalog_database.namespace, lifecycle { destroy = false } }` block handing "
                "existing databases to Phase 2 without destroying them"
            )
    else:
        errors.append(f"missing {glue_main_path}")

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    return CheckResult(name, ok=True, detail=f"{environments_checked} environment(s) validated")


_ENV_TO_PROVIDER = {"local": "local", "azure-poc": "azure", "aws-poc": "aws"}


def _installed_contract_environ(env: str) -> dict[str, str] | None:
    """Overlay `OPENLAKEFORGE_TERRAFORM_{STATE,DATA}_ROOT` for an installed
    distribution, matching `DeploymentContext.command_env()` exactly.

    `load_provider_contracts()` defaults to `os.environ`, which never has
    these vars outside an active `olf deploy` invocation. Without this, an
    installed distribution's `terraform output` reads the read-only
    payload's absent default state instead of what `olf deploy` wrote under
    `OLF_HOME`, and every environment is silently reported as unapplied
    (see `olf.tooling.terraform.external_state_options`). Returns `None`
    for a source checkout, where Terraform's own directory-relative default
    state is correct and `load_provider_contracts` already handles it.
    """
    import os

    from olf.deployment.context import DeploymentContext
    from olf.distribution import runtime_layout

    layout = runtime_layout()
    if layout.is_source:
        return None
    context = DeploymentContext.for_provider(
        _ENV_TO_PROVIDER[env],
        repo_root=layout.project_root,
        distribution_root=layout.distribution_root,
        state_root=layout.state_root,
        work_root=layout.work_root,
        cache_root=layout.cache_root,
    )
    return context.command_env(base=os.environ)


def _check_hcl_phase_two_invariants(repo_root: Path) -> CheckResult:
    """Tier 3: assert Phase-2-forbidden fields are absent from the *resolved*
    `provider_contracts.catalog` value, reusing the already-existing
    `contracts.load_provider_contracts()` (reads `terraform output -json` from
    applied state). Skips (does not fail) when no environment has been
    applied yet -- the normal case for a bare CI checkout -- matching
    `contracts.env`'s own fallback behavior."""
    name = "hcl_phase_two_invariants"
    errors: list[str] = []
    checked_environments: list[str] = []
    skipped_environments: list[str] = []

    for env in _ENVIRONMENT_ROOTS:
        terraform_dir = repo_root / "infra/terraform/environments" / env
        contracts = contracts_module.load_provider_contracts(
            str(terraform_dir), environ=_installed_contract_environ(env)
        )
        if contracts is None:
            skipped_environments.append(env)
            continue
        checked_environments.append(env)
        # v2 exposes one catalog binding; v3 one per enabled stage. Both are
        # subject to the same Phase-2 ownership rule.
        stages = contracts.get("stages")
        catalogs = (
            {stage_name: stage.get("catalog") for stage_name, stage in stages.items()}
            if isinstance(stages, dict)
            else {"": contracts.get("catalog")}
        )
        for stage_name, catalog in catalogs.items():
            if not isinstance(catalog, dict):
                located = f"stages.{stage_name}." if stage_name else ""
                errors.append(f"{env}: applied provider_contracts is missing a '{located}catalog' entry")
                continue
            for forbidden_field in _FORBIDDEN_PHASE_TWO_FIELDS:
                if forbidden_field in catalog:
                    located = f"stages.{stage_name}.catalog" if stage_name else "catalog"
                    errors.append(
                        f"{env}: applied provider_contracts.{located} resolves Phase-2-owned field "
                        f"{forbidden_field!r} (ADR 0002 violation)"
                    )

    if errors:
        return CheckResult(name, ok=False, detail="; ".join(errors))
    if checked_environments:
        detail = f"{len(checked_environments)} applied environment(s) validated"
        if skipped_environments:
            detail += f"; skipped (no applied state): {', '.join(skipped_environments)}"
        return CheckResult(name, ok=True, detail=detail)
    return CheckResult(name, ok=True, detail="skipped: no applied Terraform state for any environment")
