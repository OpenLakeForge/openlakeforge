"""Cloud (AWS/Azure) platform teardown.

Port of `scripts/{aws,azure}/stack/teardown.sh`. Destroys the
Terraform-managed platform (Helm releases, namespace) while leaving the
foundation (EKS/AKS, ECR/ACR) running; foundation teardown is a separate
step (`olf.deployment.cloud.foundation`).
"""

from __future__ import annotations

from collections.abc import Mapping

from olf import log
from olf.deployment.cloud.backend import CloudBackend, FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.tooling.toolkit import Toolkit


def platform_down(
    config: CloudDeploymentConfig,
    tools: Toolkit,
    backend: CloudBackend,
    facts: FoundationFacts,
    *,
    env: Mapping[str, str],
) -> None:
    platform_dir = config.paths.platform_terraform_dir

    log.step("Removing completed Superset init hook job if present...")
    tools.kubectl.delete(
        "job",
        "superset-init-db",
        namespace=config.namespace,
        context=facts.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        ignore_not_found=True,
        extra_args=("--wait=true",),
        env=env,
    )

    log.step(f"Destroying Terraform {backend.scope} platform...")
    variables = backend.platform_destroy_variables(config, facts)
    var_files = (str(config.terraform.var_file),) if config.terraform.var_file is not None else ()
    tools.terraform.init(platform_dir, env=env)
    tools.terraform.destroy(platform_dir, var_files=var_files, variables=variables, env=env)

    log.step(f"Deleting namespace '{config.namespace}' if it still exists...")
    tools.kubectl.delete(
        "namespace",
        config.namespace,
        context=facts.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        ignore_not_found=True,
        env=env,
    )

    log.step(f"{backend.scope.capitalize()} platform teardown complete.")
