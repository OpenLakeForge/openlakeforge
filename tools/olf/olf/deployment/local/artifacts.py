"""Dynamic local artifact deployment.

Port of `scripts/local/stack/deploy-artifacts.sh`. Calls the existing Python
library functions directly (in-process) instead of shelling out to
`uv run olf ...` or sourcing `scripts/contracts/load-runtime-env.sh`.
Preserves the exact ordering: load provider contracts -> reconcile catalog
namespaces -> build/load the project-code image -> point Dagster at the
image -> deploy optional Superset/OpenMetadata artifacts.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from olf import k8s, log
from olf.deployment import contract_env
from olf.deployment.artifact_steps import (
    ArtifactOperationError,
    deploy_optional_layer_artifacts,
    sync_catalog_namespaces,
)
from olf.deployment.engine import Toolkit
from olf.deployment.local import images
from olf.deployment.local.config import LocalDeploymentConfig

__all__ = [
    "ArtifactOperationError",
    "applied_contract_environment",
    "artifacts_deploy",
    "deploy_optional_layer_artifacts",
    "sync_catalog_namespaces",
]


@contextmanager
def applied_contract_environment(
    config: LocalDeploymentConfig,
    *,
    contract_terraform_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Iterator[dict[str, str]]:
    """Apply the resolved provider-contract environment onto `os.environ`.

    Thin local-config adapter over `olf.deployment.contract_env`, which
    replaces sourcing `scripts/contracts/load-runtime-env.sh`. `Deployment
    Context.command_env` itself never mutates `os.environ` - this is the
    documented, deliberate bridge to the existing `os.environ`-reading
    library modules (`olf.k8s`, `olf.config`, `olf.s3`, `olf.layers`).
    """
    resolved_contract_terraform_dir = contract_terraform_dir or Path(
        (environ or os.environ).get("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", config.paths.platform_terraform_dir)
    ).resolve()
    with contract_env.applied_contract_environment(
        contract_terraform_dir=resolved_contract_terraform_dir,
        repo_root=config.paths.repo_root,
        namespace=config.namespace,
        kube_context=config.kube_context,
        kubeconfig_path=config.paths.kubeconfig_path,
        port_forward_log_prefix=config.paths.port_forward_log_prefix,
        environ=environ,
        topology=config.context.topology,
        stage=config.context.stage,
    ) as env:
        yield env


def artifacts_deploy(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    with applied_contract_environment(config, environ=env or None):
        log.step("Reconciling Polaris namespaces from the domain descriptors...")
        sync_catalog_namespaces()

        images.prepare_project_code_image(config, tools, env=env)

        log.step(f"Pointing Dagster at project-code image {config.images.project_code_image}...")
        k8s.set_project_code_image(config.images.project_code_image, config.namespace)

        os.environ.setdefault("OPENMETADATA_ALLOW_MISSING_ASSETS", "true")
        deploy_optional_layer_artifacts(os.environ)

        log.step("Dynamic OpenLakeForge local artifacts are deployed.")
