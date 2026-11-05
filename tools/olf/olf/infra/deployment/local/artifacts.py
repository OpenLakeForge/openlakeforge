"""Dynamic local artifact deployment: build the project, then activate it as a revision.

`deploy_revision` owns the order inside the activation -- catalog namespaces, the optional Superset and OpenMetadata
layers, then the Dagster rollout -- so a failed optional layer leaves Dagster on its previous image.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from olf.common import log
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.infra.deployment import contract_env
from olf.infra.deployment.revision_build import activate_deploy_project

if TYPE_CHECKING:
    from olf.infra.deployment.engine import LocalDeploymentProvider

__all__ = ["applied_contract_environment", "artifacts_deploy"]


@contextmanager
def applied_contract_environment(
    config: LocalDeploymentConfig,
    *,
    contract_terraform_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Iterator[contract_env.ContractEnvironment]:
    """Yield the resolved provider-contract environment for a local deployment.

    Thin local-config adapter over `olf.infra.deployment.contract_env`, which
    replaces sourcing `scripts/contracts/load-runtime-env.sh`.
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


def artifacts_deploy(provider: LocalDeploymentProvider) -> None:
    from olf.common.profile import PUBLIC_EXAMPLE_IMAGE_REPOSITORY

    # Local has no registry of its own. A project that names a repository publishes to it like every other provider;
    # one that does not stays on this cluster, and pushing the example build would overwrite the public example.
    push = provider.config.images.project_code_repository != PUBLIC_EXAMPLE_IMAGE_REPOSITORY
    activate_deploy_project(provider, push=push)
    log.step("Dynamic OpenLakeForge local artifacts are deployed.")
