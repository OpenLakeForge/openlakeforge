"""`olf check structure`: repository skeleton, shell-script ban, release guard."""

from __future__ import annotations

import shlex
from pathlib import Path

import typer
import yaml

from olf.common.commands.checks._shared import _root
from olf.common.log import fail
from olf.common.tooling.toolkit import Toolkit

REQUIRED_PATHS: tuple[str, ...] = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "openlakeforge.yaml",
    "openlakeforge.conformance.yaml",
    ".gitignore",
    ".github/workflows/checks.yml",
    ".github/actions/build-revision/action.yml",
    ".github/actions/deploy-stage/action.yml",
    "docs/navigation.yaml",
    "skills/README.md",
    "skills/deploying-openlakeforge/SKILL.md",
    "docs/guides/promote-dev-to-prod.md",
    "docs/architecture/README.md",
    "docs/architecture/overview.md",
    "docs/architecture/floe-validation.md",
    "docs/architecture/azure-aks-poc.md",
    "docs/architecture/aws-eks-poc.md",
    "docs/architecture/provider-contracts.md",
    "docs/schema/provider-contracts.schema.json",
    "docs/schema/governance.schema.json",
    "docs/technical-debt.md",
    "docs/testing/floe-openlineage-capture-test-plan.md",
    "docs/adr/README.md",
    "docs/adr/0001-platform-baseline-and-component-stack.md",
    "docs/adr/0002-deployment-lifecycle.md",
    "docs/adr/0003-provider-contracts.md",
    "docs/adr/0004-medallion-layout-and-catalog-namespaces.md",
    "docs/adr/0005-descriptor-model.md",
    "docs/adr/0006-dagster-runtime-and-code-locations.md",
    "docs/adr/0007-governance-and-lineage.md",
    "docs/adr/0008-olf-owns-orchestration-and-toolchain.md",
    "docs/adr/0009-distribution.md",
    "docs/adr/0010-cloud-provider-implementations.md",
    "docs/adr/0011-deployment-profile-and-stages.md",
    "docs/adr/0012-project-revisions-and-promotion.md",
    "docs/adr/0013-service-access-and-ingress.md",
    "docs/adr/0014-identity-and-authorization.md",
    "docs/adr/0015-polaris-identity-provider-evaluation.md",
    "docs/adr/0016-trino-workload-authentication.md",
    "docs/adr/0017-local-network-policy-and-rbac.md",
    "docs/architecture/identity/identity-service-permissions.md",
    "docs/testing/trino_workload_auth_proof.py",
    "infra/terraform/modules/query/trino/certificate/Chart.yaml",
    "infra/terraform/modules/query/trino/certificate/templates/certificate.yaml",
    "release/identity-roles.yaml",
    "release/query-sizes.yaml",
    "release/network-flows.yaml",
    "release/storage-sizes.yaml",
    "infra/terraform/modules/security/network-policy/main.tf",
    "infra/terraform/modules/security/network-policy/variables.tf",
    "infra/terraform/modules/security/network-policy/outputs.tf",
    "docs/setup/github-oauth-app.md",
    "infra/README.md",
    "infra/terraform/README.md",
    "infra/terraform/environments/local/contracts.tf",
    "infra/terraform/environments/azure-poc/main.tf",
    "infra/terraform/environments/azure-poc/variables.tf",
    "infra/terraform/environments/azure-poc/outputs.tf",
    "infra/terraform/environments/azure-poc/contracts.tf",
    "infra/terraform/environments/aws-poc/main.tf",
    "infra/terraform/environments/aws-poc/variables.tf",
    "infra/terraform/environments/aws-poc/outputs.tf",
    "infra/terraform/environments/aws-poc/contracts.tf",
    "infra/terraform/foundations/local-kind/main.tf",
    "infra/terraform/foundations/local-kind/variables.tf",
    "infra/terraform/foundations/local-kind/outputs.tf",
    "infra/terraform/foundations/azure-aks/main.tf",
    "infra/terraform/foundations/azure-aks/variables.tf",
    "infra/terraform/foundations/azure-aks/outputs.tf",
    "infra/terraform/foundations/aws-eks/main.tf",
    "infra/terraform/foundations/aws-eks/variables.tf",
    "infra/terraform/foundations/aws-eks/outputs.tf",
    "infra/terraform/modules/storage/aws-s3/main.tf",
    "infra/terraform/modules/storage/aws-s3/variables.tf",
    "infra/terraform/modules/storage/aws-s3/outputs.tf",
    "infra/terraform/modules/storage/rds-postgresql/main.tf",
    "infra/terraform/modules/storage/rds-postgresql/variables.tf",
    "infra/terraform/modules/storage/rds-postgresql/outputs.tf",
    "infra/terraform/modules/catalog/aws-glue/main.tf",
    "infra/terraform/modules/catalog/aws-glue/variables.tf",
    "infra/terraform/modules/catalog/aws-glue/outputs.tf",
    "infra/terraform/modules/access/traefik/main.tf",
    "infra/terraform/modules/access/traefik/variables.tf",
    "infra/terraform/modules/access/traefik/outputs.tf",
    "infra/terraform/modules/access/portal/main.tf",
    "infra/terraform/modules/access/portal/variables.tf",
    "infra/terraform/modules/access/portal/outputs.tf",
    "infra/terraform/modules/access/perimeter/main.tf",
    "infra/terraform/modules/access/perimeter/variables.tf",
    "infra/terraform/modules/access/perimeter/outputs.tf",
    "infra/terraform/modules/access/cert-manager/main.tf",
    "infra/terraform/modules/access/cert-manager/variables.tf",
    "infra/terraform/modules/access/cert-manager/outputs.tf",
    "infra/terraform/modules/access/cert-manager/local-ca/Chart.yaml",
    "infra/terraform/modules/access/cert-manager/local-ca/templates/issuers.yaml",
    "infra/terraform/modules/access/in-cluster-resolution/main.tf",
    "infra/terraform/modules/access/in-cluster-resolution/variables.tf",
    "infra/terraform/modules/access/in-cluster-resolution/outputs.tf",
    "infra/terraform/modules/access/in-cluster-resolution/templates/publish-ca.sh.tftpl",
    "infra/terraform/modules/identity/dex/main.tf",
    "infra/terraform/modules/identity/dex/credentials.tf",
    "infra/terraform/modules/identity/dex/variables.tf",
    "infra/terraform/modules/identity/dex/outputs.tf",
    "infra/terraform/modules/identity/dex/templates/credentials.sh.tftpl",
    "infra/helm/values/local/traefik.yaml",
    "infra/helm/values/local/cert-manager.yaml",
    "infra/helm/README.md",
    "infra/helm/values/local/dagster.yaml",
    "infra/helm/values/local/superset.yaml",
    "images/README.md",
    "images/project-code/README.md",
    "images/project-code/Dockerfile",
    "images/project-code/pyproject.toml",
    "images/superset/README.md",
    "images/superset/Dockerfile",
    "libs/README.md",
    "libs/__init__.py",
    "libs/bronze_csv.py",
    "libs/floe_profile.py",
    "libs/k8s_log_archive.py",
    "libs/openlakeforge_logging.py",
    "libs/dbt/__init__.py",
    "libs/dbt/openlakeforge_dbt/dbt_project.yml",
    "libs/dbt/openlakeforge_dbt/macros/generate_schema_name.sql",
    "libs/dbt/profiles/trino.yml",
    "libs/dbt/render_profiles.py",
    "libs/product_dagster.py",
    "libs/s3_artifacts.py",
    "lakehouse_code/__init__.py",
    "lakehouse_code/definitions.py",
    "lakehouse_code/governance.yml",
    "lakehouse_code/bronze/__init__.py",
    "lakehouse_code/bronze/crm/source.yaml",
    "lakehouse_code/bronze/crm/dlt/crm.py",
    "lakehouse_code/bronze/crm/examples/orders.csv",
    "lakehouse_code/bronze/crm/examples/accounts.csv",
    "lakehouse_code/bronze/erp/source.yaml",
    "lakehouse_code/bronze/erp/dlt/erp.py",
    "lakehouse_code/bronze/erp/examples/inventory_snapshots.csv",
    "lakehouse_code/pipelines/__init__.py",
    "lakehouse_code/pipelines/dagster/__init__.py",
    "lakehouse_code/pipelines/dagster/order_revenue.py",
    "lakehouse_code/pipelines/dagster/customer_health.py",
    "lakehouse_code/pipelines/dagster/inventory_reliability.py",
    "lakehouse_code/silver/__init__.py",
    "lakehouse_code/silver/floe.yml",
    "lakehouse_code/silver/sources.yml",
    "lakehouse_code/silver/sales/__init__.py",
    "lakehouse_code/silver/sales/contracts/floe/_domain.yml",
    "lakehouse_code/silver/supply_chain/__init__.py",
    "lakehouse_code/silver/supply_chain/contracts/floe/_domain.yml",
    "lakehouse_code/gold/__init__.py",
    "lakehouse_code/gold/order_revenue/__init__.py",
    "lakehouse_code/gold/order_revenue/dbt/dbt_project.yml",
    "lakehouse_code/gold/order_revenue/dbt/packages.yml",
    "lakehouse_code/gold/customer_health/__init__.py",
    "lakehouse_code/gold/customer_health/dbt/dbt_project.yml",
    "lakehouse_code/gold/customer_health/dbt/packages.yml",
    "lakehouse_code/gold/inventory_reliability/__init__.py",
    "lakehouse_code/gold/inventory_reliability/dbt/dbt_project.yml",
    "lakehouse_code/gold/inventory_reliability/dbt/packages.yml",
    "lakehouse_code/dashboards/__init__.py",
    "lakehouse_code/dashboards/superset/__init__.py",
    "lakehouse_code/dashboards/superset/sales_order_revenue/metadata.yaml",
    "lakehouse_code/dashboards/superset/sales_order_revenue/charts/Daily_Net_Revenue_1.yaml",
    "lakehouse_code/dashboards/superset/sales_customer_health/metadata.yaml",
    "lakehouse_code/dashboards/superset/sales_customer_health/charts/Health_Score_by_Segment_1.yaml",
    "lakehouse_code/dashboards/superset/supply_chain_inventory_reliability/metadata.yaml",
    "lakehouse_code/dashboards/superset/supply_chain_inventory_reliability/charts/Available_Inventory_by_Status_1.yaml",
    "tools/olf/pyproject.toml",
    "tools/olf/uv.lock",
    "tools/olf/olf/cli.py",
    "tools/olf/olf/common/project.py",
    "tools/olf/olf/infra/commands/project.py",
    "tools/olf/olf/common/commands/checks/__init__.py",
    "tools/olf/olf/common/commands/checks/_components.py",
    "tools/olf/olf/common/commands/checks/_contracts.py",
    "tools/olf/olf/common/commands/checks/_dbt.py",
    "tools/olf/olf/common/commands/checks/_docs.py",
    "tools/olf/olf/common/commands/checks/_infra.py",
    "tools/olf/olf/common/commands/checks/_project_code.py",
    "tools/olf/olf/common/commands/checks/_shared.py",
    "tools/olf/olf/common/commands/checks/_skills.py",
    "tools/olf/olf/common/commands/checks/_structure.py",
    "tools/olf/olf/common/profile.py",
    "tools/olf/olf/common/commands/profile.py",
    "tools/olf/olf/common/initialization.py",
    "tools/olf/olf/infra/contracts.py",
    "tools/olf/olf/common/contracts_check/__init__.py",
    "tools/olf/olf/common/contracts_check/_report.py",
    "tools/olf/olf/common/contracts_check/_descriptors.py",
    "tools/olf/olf/common/contracts_check/_hcl.py",
    "tools/olf/olf/common/contracts_check/_rendered.py",
    "tools/olf/olf/infra/catalog.py",
    "tools/olf/olf/infra/glue.py",
    "tools/olf/olf/infra/polaris.py",
    "packages/domain-model/pyproject.toml",
    "packages/domain-model/openlakeforge_domain/__init__.py",
    "packages/domain-model/openlakeforge_domain/descriptors.py",
    "packages/domain-model/openlakeforge_domain/inventory.py",
    "packages/lakehouse-services/pyproject.toml",
    "packages/lakehouse-services/openlakeforge_services/__init__.py",
    "packages/lakehouse-services/openlakeforge_services/py.typed",
    "tools/olf/olf/infra/k8s.py",
    "tools/olf/olf/infra/e2e/__init__.py",
    "tools/olf/olf/infra/s3.py",
    "tools/olf/olf/infra/superset.py",
    "tools/olf/olf/infra/access.py",
    "tools/olf/olf/infra/commands/access.py",
    "tools/olf/olf/infra/commands/query.py",
    "tools/olf/olf/infra/trino.py",
    "tools/olf/olf/infra/commands/github.py",
    "tools/olf/olf/infra/github_app.py",
    "tools/olf/olf/infra/commands/secrets.py",
    "tools/olf/olf/infra/secrets_rotation.py",
    "tools/olf/olf/infra/e2e/_rotation.py",
    "tools/olf/olf/infra/openmetadata/__init__.py",
    "tools/olf/olf/common/scaffold/__init__.py",
    "tools/olf/olf/common/scaffold/_shared.py",
    "tools/olf/olf/common/scaffold/_yaml_edit.py",
    "tools/olf/olf/common/scaffold/_commit.py",
    "tools/olf/olf/common/scaffold/_csv.py",
    "tools/olf/olf/common/scaffold/_templates.py",
    "tools/olf/olf/common/scaffold/source.py",
    "tools/olf/olf/common/scaffold/domain.py",
    "tools/olf/olf/common/scaffold/product.py",
    "tools/olf/olf/common/commands/source.py",
    "tools/olf/olf/common/commands/domain.py",
    "tools/olf/olf/infra/commands/product.py",
    "tools/olf/olf/common/commands/init.py",
    "tools/olf/olf/infra/deployment/engine.py",
    "tools/olf/olf/infra/deployment/local/provider.py",
    "tools/olf/olf/common/deployment/local/config.py",
    "tools/olf/olf/infra/deployment/cloud/provider.py",
    "tools/olf/olf/infra/deployment/cloud/config.py",
    "tools/olf/olf/infra/deployment/cloud/backend.py",
    "tools/olf/olf/infra/deployment/cloud/aws.py",
    "tools/olf/olf/infra/deployment/cloud/azure.py",
    "tools/olf/olf/infra/deployment/cloud/foundation.py",
    "tools/olf/olf/infra/deployment/cloud/platform.py",
    "tools/olf/olf/infra/deployment/cloud/artifacts.py",
    "tools/olf/olf/infra/deployment/cloud/teardown.py",
    "tools/olf/olf/infra/deployment/cloud/forward.py",
    "tools/olf/olf/infra/deployment/cloud/images.py",
    "tools/olf/olf/infra/deployment/artifact_steps.py",
    "tools/olf/olf/infra/deployment/contract_env.py",
    "tools/olf/olf/common/deployment/env_settings.py",
    "tools/olf/olf/infra/tooling/aws.py",
    "tools/olf/olf/infra/tooling/azure.py",
    ".github/workflows/release.yml",
    ".github/workflows/security.yml",
    ".trivyignore.yaml",
    "docs/adr/0008-olf-owns-orchestration-and-toolchain.md",
)


def _missing_required_paths(root: Path) -> list[str]:
    """Return required skeleton entries absent from a checkout."""
    return [path for path in REQUIRED_PATHS if not (root / path).exists()]


def structure(repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate.")) -> None:
    """Validate the essential repository skeleton and prohibit shell scripts."""
    from olf.common.dashboard_checks import validate_superset_assets

    root = _root(repo_root)
    missing = _missing_required_paths(root)
    modules_root = root / "infra/terraform/modules"
    module_errors: list[str] = []
    if modules_root.is_dir():
        module_dirs = {path.parent for path in modules_root.rglob("*.tf")}
        for module in sorted(module_dirs):
            for filename in ("main.tf", "variables.tf", "outputs.tf"):
                if not (module / filename).is_file():
                    module_errors.append(f"Terraform module {module.relative_to(root)} is missing {filename}")
    tracked = Toolkit.default().runner.run(["git", "ls-files", "-z"], cwd=root).stdout.split("\0")
    scripts = [root / path for path in tracked if path.endswith(".sh") and (root / path).is_file()]
    release_workflow_errors = _release_publication_guard_errors(root)
    if missing or scripts or module_errors or release_workflow_errors:
        details = [
            *(f"missing required path: {path}" for path in missing),
            *module_errors,
            *(f"shell script is forbidden: {path.relative_to(root)}" for path in scripts),
            *release_workflow_errors,
        ]
        raise typer.Exit(code=fail("\n".join(details)))
    dashboard_errors = validate_superset_assets(root)
    if dashboard_errors:
        raise typer.Exit(code=fail("\n".join(dashboard_errors)))
    typer.echo("Repository structure is valid.")


def _release_publication_guard_errors(root: Path) -> list[str]:
    """Require release publication to delegate its main/checks guard to ``olf``."""
    workflow_path = root / ".github/workflows/release.yml"
    try:
        workflow = yaml.safe_load(workflow_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot parse release workflow: {exc}"]
    if not isinstance(workflow, dict):
        return ["release workflow must be a YAML mapping"]
    jobs = workflow.get("jobs")
    prepare = jobs.get("prepare") if isinstance(jobs, dict) else None
    steps = prepare.get("steps") if isinstance(prepare, dict) else None
    if not isinstance(steps, list):
        return ["release workflow must define prepare steps"]
    guard = next(
        (
            step
            for step in steps
            if isinstance(step, dict) and step.get("name") == "Require a green main commit before publishing"
        ),
        None,
    )
    if not isinstance(guard, dict):
        return ["release workflow must require a green main commit before publishing"]
    if guard.get("if") != "steps.mode.outputs.dry_run == 'false'":
        return ["release workflow green-main guard must run for non-dry releases only"]
    environment = guard.get("env")
    if not isinstance(environment, dict) or environment.get("GH_TOKEN") != "${{ github.token }}":
        return ["release workflow green-main guard must provide GH_TOKEN"]
    command = guard.get("run")
    if not isinstance(command, str):
        return ["release workflow green-main guard must invoke olf"]
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return [f"release workflow green-main guard has invalid command syntax: {exc}"]
    expected = ["uv", "run", "--project", "tools/olf", "--locked", "olf", "release", "workflow", "require-green-main"]
    has_expected_command = argv[: len(expected)] == expected
    has_repository = _option_value(argv, "--repo") == "${{ github.repository }}"
    has_sha = _option_value(argv, "--sha") == "${{ github.sha }}"
    if not (has_expected_command and has_repository and has_sha):
        return ["release workflow green-main guard must delegate repository and SHA validation to olf"]
    return []


def _option_value(argv: list[str], option: str) -> str | None:
    """Return a long option's following argument from a structured workflow command."""
    try:
        return argv[argv.index(option) + 1]
    except (ValueError, IndexError):
        return None
