"""E2E configuration/runner: environment setup, suite dispatch, and the
full-suite assertion inventory.

Composition root of the `olf.e2e` package -- it is the only submodule that
imports from every capability module, to sequence them into `run()`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openlakeforge_domain import inventory_for

from olf import log
from olf.e2e._artifacts import check_ops_artifacts
from olf.e2e._assertions import check_openmetadata_assets, check_superset_dashboards
from olf.e2e._dagster import launch_and_poll_dagster_jobs
from olf.e2e._health import check_pods_ready
from olf.e2e._ingress import (
    check_certificate_renewal,
    check_portal_lists_routes,
    check_traefik_restart_recovery,
)
from olf.e2e._isolation import check_stage_isolation
from olf.e2e._layers import Layer, configured_layers
from olf.e2e._preflight import check_aws_provider_contracts, check_aws_storage_and_glue
from olf.e2e._shell import (
    E2EConfig,
    E2EError,
    Environment,
    Suite,
    _kubectl_executable,
    _run,
    _run_retry,
    terraform_output,
)
from olf.e2e._trino import (
    check_catalog_namespaces,
    check_polaris_restart_recovery,
    check_trino_catalog,
    check_trino_product_tables_and_marts,
    check_trino_tables_and_marts,
)
from olf.project import ProjectSpec


@dataclass(frozen=True)
class FullAssertion:
    """One full-suite assertion and the optional platform layer it needs."""

    label: str
    check: Callable[[E2EConfig], None]
    layer: Layer | None = None


def run(
    env: Environment,
    *,
    suite: Suite | None = None,
    namespace: str = "olf-dev",
    shared_namespace: str = "",
    kube_context: str = "",
    repo_root: Path | None = None,
    distribution_root: Path | None = None,
) -> None:
    cfg = prepare_config(
        env,
        suite=suite,
        namespace=namespace,
        shared_namespace=shared_namespace,
        kube_context=kube_context,
        repo_root=repo_root,
        distribution_root=distribution_root,
    )
    check_commands(cfg)
    prepare_kube_context(cfg)
    check_pods_ready(cfg)
    if cfg.suite == "smoke":
        run_smoke(cfg)
    else:
        if cfg.env == "aws":
            run_smoke(cfg)
        run_full(cfg)
    log.info(f"{cfg.env.capitalize()} OpenLakeForge {cfg.suite} e2e validation passed.")


def default_suite(_env: Environment) -> Suite:
    return "full"


def prepare_config(
    env: Environment,
    *,
    suite: Suite | None,
    namespace: str,
    kube_context: str,
    shared_namespace: str = "",
    repo_root: Path | None,
    distribution_root: Path | None = None,
) -> E2EConfig:
    """`repo_root` (descriptor discovery, Superset report dirs) and
    `distribution_root` (foundation/contract Terraform roots) are distinct:
    an installed distribution's writable project root defaults to the
    bundled demo, while its Terraform roots live in the read-only payload
    extracted under `OLF_HOME` - they only coincide in source-mode
    checkouts, where `distribution_root` defaults to `repo_root`.
    """
    legacy_project_root = os.environ.get("OPENLAKEFORGE_REPO_ROOT", ".")
    default_project_root = Path(os.environ.get("OPENLAKEFORGE_PROJECT_ROOT", legacy_project_root))
    default_distribution_root = Path(os.environ.get("OLF_DISTRIBUTION_ROOT", default_project_root))
    project = ProjectSpec(
        root=repo_root or default_project_root,
        distribution_root=distribution_root or repo_root or default_distribution_root,
    )
    root = project.root
    dist_root = project.distribution_root
    actual_suite = suite or default_suite(env)
    inventory = inventory_for(root)
    if env == "local":
        cluster_name = os.environ.get("CLUSTER_NAME", "openlakeforge-local")
        return E2EConfig(
            env=env,
            suite=actual_suite,
            namespace=namespace,
            shared_namespace=shared_namespace,
            kube_context=kube_context or os.environ.get("KUBE_CONTEXT", f"kind-{cluster_name}"),
            repo_root=root,
            distribution_root=dist_root,
            foundation_terraform_dir=dist_root / "infra/terraform/foundations/local-kind",
            contract_terraform_dir=_contract_dir(dist_root, "infra/terraform/environments/local"),
            inventory=inventory,
            dagster_local_port=int(os.environ.get("DAGSTER_LOCAL_PORT", "13000")),
            superset_local_port=int(os.environ.get("SUPERSET_LOCAL_PORT", "18088")),
            openmetadata_local_port=int(os.environ.get("OPENMETADATA_LOCAL_PORT", "18585")),
            seaweedfs_local_port=int(os.environ.get("SEAWEEDFS_LOCAL_PORT", "19000")),
        )
    if env == "azure":
        cluster_name = os.environ.get("AZURE_CLUSTER_NAME", "aks-openlakeforge-poc")
        return E2EConfig(
            env=env,
            suite=actual_suite,
            namespace=namespace,
            shared_namespace=shared_namespace,
            kube_context=kube_context or os.environ.get("KUBE_CONTEXT", cluster_name),
            repo_root=root,
            distribution_root=dist_root,
            foundation_terraform_dir=dist_root / "infra/terraform/foundations/azure-aks",
            contract_terraform_dir=_contract_dir(dist_root, "infra/terraform/environments/azure-poc"),
            inventory=inventory,
            dagster_local_port=int(os.environ.get("DAGSTER_LOCAL_PORT", "13000")),
            superset_local_port=int(os.environ.get("SUPERSET_LOCAL_PORT", "18088")),
            openmetadata_local_port=int(os.environ.get("OPENMETADATA_LOCAL_PORT", "18585")),
            seaweedfs_local_port=int(os.environ.get("SEAWEEDFS_LOCAL_PORT", "19000")),
        )
    if env == "aws":
        cluster_name = os.environ.get("AWS_CLUSTER_NAME", "limited-eks-openlakeforge-poc")
        return E2EConfig(
            env=env,
            suite=actual_suite,
            namespace=namespace,
            shared_namespace=shared_namespace,
            kube_context=kube_context or os.environ.get("KUBE_CONTEXT", cluster_name),
            repo_root=root,
            distribution_root=dist_root,
            foundation_terraform_dir=dist_root / "infra/terraform/foundations/aws-eks",
            contract_terraform_dir=_contract_dir(dist_root, "infra/terraform/environments/aws-poc"),
            inventory=inventory,
            aws_region=os.environ.get("AWS_REGION"),
            dagster_local_port=int(os.environ.get("DAGSTER_LOCAL_PORT", "13000")),
            superset_local_port=int(os.environ.get("SUPERSET_LOCAL_PORT", "18088")),
            openmetadata_local_port=int(os.environ.get("OPENMETADATA_LOCAL_PORT", "18585")),
        )
    raise E2EError(f"unsupported e2e environment: {env}")


def _contract_dir(distribution_root: Path, default: str) -> Path:
    return Path(os.environ.get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", distribution_root / default)).resolve()


def check_commands(cfg: E2EConfig) -> None:
    from olf.deployment.errors import ExecutableNotFoundError, ToolchainError
    from olf.tooling.resolver import build_resolver

    missing: list[str] = []
    resolver = build_resolver()
    for managed_tool in ("kubectl", "terraform"):
        try:
            resolver.resolve(managed_tool)
        except ExecutableNotFoundError:
            missing.append(managed_tool)
        except ToolchainError as exc:
            # A managed tool that couldn't be provisioned (bad digest,
            # broken download, unwritable cache) is a different failure
            # from "not found", but `olf e2e run` only catches E2EError -
            # surface it there too, with the actionable reason, instead of
            # letting it escape as a raw traceback.
            missing.append(f"{managed_tool} ({exc})")

    if missing:
        raise E2EError(f"missing required command(s): {', '.join(missing)}")


def prepare_kube_context(cfg: E2EConfig) -> None:
    kubeconfig = configure_kubeconfig(cfg)
    if cfg.env == "local" and kube_context_is_ready(cfg.kube_context):
        return

    if cfg.env == "azure":
        if cfg.foundation_terraform_dir is None:
            raise E2EError("Azure e2e requires a foundation Terraform directory.")
        resource_group = terraform_output(cfg.foundation_terraform_dir, "resource_group_name")
        cluster_name = terraform_output(cfg.foundation_terraform_dir, "cluster_name")
        from olf.tooling.azure import AzureSdk

        AzureSdk().aks_get_credentials(
            cluster_name, resource_group=resource_group, kubeconfig_path=kubeconfig, overwrite=True
        )
    elif cfg.env == "aws":
        if cfg.foundation_terraform_dir is None:
            raise E2EError("AWS e2e requires a foundation Terraform directory.")
        region = terraform_output(cfg.foundation_terraform_dir, "aws_region")
        cluster_name = terraform_output(cfg.foundation_terraform_dir, "cluster_name")
        from olf.tooling.aws import AwsSdk

        AwsSdk().eks_update_kubeconfig(
            cluster_name, region=region, kubeconfig_path=kubeconfig, alias=cfg.kube_context
        )
    _run_retry(
        [_kubectl_executable(), "cluster-info", "--context", cfg.kube_context], capture=True, attempts=6, delay=5
    )


def configure_kubeconfig(cfg: E2EConfig) -> Path:
    provider_override = os.environ.get(f"{cfg.env.upper()}_KUBECONFIG_PATH")
    configured_path = os.environ.get("KUBECONFIG") or provider_override
    kubeconfig = (
        Path(configured_path).expanduser()
        if configured_path
        else cfg.repo_root / ".tmp/kubeconfigs" / f"{cfg.env}.yaml"
    )
    kubeconfig = kubeconfig.resolve()
    kubeconfig.parent.mkdir(parents=True, exist_ok=True)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    return kubeconfig


def kube_context_is_ready(kube_context: str) -> bool:
    try:
        _run([_kubectl_executable(), "cluster-info", "--context", kube_context], capture=True)
    except E2EError:
        return False
    return True


def run_smoke(cfg: E2EConfig) -> None:
    if cfg.env == "aws":
        check_aws_provider_contracts(cfg)
        check_aws_storage_and_glue(cfg)
    check_trino_catalog(cfg)
    check_catalog_namespaces(cfg)
    if cfg.env == "local":
        product = cfg.inventory.default_product
        log.step(f"Running smoke path for descriptor-selected product {product.id}...")
        launch_and_poll_dagster_jobs(cfg, products=(product,))
        check_trino_product_tables_and_marts(cfg, product)


def run_full(cfg: E2EConfig) -> None:
    enabled_layers = configured_layers(cfg)
    skipped: list[str] = []
    for assertion in full_assertions(cfg):
        if assertion.layer is not None and not enabled_layers[assertion.layer]:
            skipped.append(assertion.label)
            log.info(f"Skipping {assertion.label}: {assertion.layer} layer is disabled.")
            continue
        assertion.check(cfg)
    if skipped:
        log.info("Skipped e2e assertions: " + ", ".join(skipped))


def full_assertions(cfg: E2EConfig) -> tuple[FullAssertion, ...]:
    """Return the full-suite assertion inventory for the deployed profile."""
    assertions = [
        FullAssertion("Polaris namespaces", check_catalog_namespaces),
        FullAssertion("Dagster product pipelines", launch_and_poll_dagster_jobs),
        FullAssertion("Silver and Gold tables", check_trino_tables_and_marts),
    ]
    if cfg.env == "local":
        assertions.append(FullAssertion("Polaris restart recovery", check_polaris_restart_recovery))
        assertions.append(FullAssertion("Traefik restart recovery", check_traefik_restart_recovery))
        assertions.append(FullAssertion("route certificate renewal", check_certificate_renewal))
        assertions.append(FullAssertion("landing page routes", check_portal_lists_routes))
    assertions.extend(
        [
            FullAssertion("Superset dashboards", check_superset_dashboards, layer="analytics"),
            FullAssertion("OpenMetadata governance assets", check_openmetadata_assets, layer="governance"),
            FullAssertion("ops bucket artifacts", check_ops_artifacts),
            FullAssertion("cross-stage isolation", check_stage_isolation),
        ]
    )
    return tuple(assertions)
