"""Local platform teardown.

Port of `scripts/local/stack/teardown.sh`. Destroys the Terraform-managed
platform (Helm releases, namespaces) while leaving the kind cluster running;
foundation teardown is a separate step (`olf.deployment.local.foundation`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from olf import log
from olf.deployment import kube_ops
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentPreconditionError, ExecutableNotFoundError
from olf.deployment.local.config import LocalDeploymentConfig
from olf.deployment.local.platform import platform_destroy_variables, platform_var_files

LEGACY_HELM_RELEASES = ("trino", "polaris", "seaweedfs", "garage")

# The kubeadm Corefile the foundation's CoreDNS starts with: the same as
# modules/access/in-cluster-resolution minus its `rewrite` line. Destroying that
# module removes the whole key, so a platform-only teardown puts this back.
_STOCK_COREFILE = """.:53 {
    errors
    health {
       lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
       ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf {
       max_concurrent 1000
    }
    cache 30 {
       disable success cluster.local
       disable denial cluster.local
    }
    loop
    reload
    loadbalance
}
"""


def owned_namespaces(config: LocalDeploymentConfig) -> tuple[str, ...]:
    """Every namespace this deployment created. See `DeploymentContext`."""
    return config.context.owned_namespaces


def managed_namespaces(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> tuple[str, ...]:
    """Namespaces labelled as belonging to this deployment, including a stage
    that was deployed and has since been disabled. See
    `kube_ops.managed_namespaces`, which the foundation guard shares."""
    return kube_ops.managed_namespaces(
        tools.kubectl,
        profile_name=config.context.topology.profile_name,
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    )


def cleanup_legacy_helm_releases(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> list[str]:
    """Remove unmanaged Helm releases from stacks predating Terraform ownership."""
    removed: list[str] = []
    for release in LEGACY_HELM_RELEASES:
        try:
            status = tools.helm.status(
                release, namespace=config.context.shared_namespace, kube_context=config.kube_context, env=env
            )
        except ExecutableNotFoundError:
            log.warn("'helm' not found on PATH; skipping legacy Helm release cleanup.")
            return removed
        if status.ok:
            tools.helm.uninstall(
                release, namespace=config.context.shared_namespace, kube_context=config.kube_context, env=env
            )
            removed.append(release)
    return removed


def platform_down(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> None:
    platform_dir = config.paths.platform_terraform_dir

    cluster_reachable = tools.kubectl.cluster_info(
        context=config.kube_context, kubeconfig=config.paths.kubeconfig_path, env=env, check=False
    ).ok
    if not cluster_reachable:
        raise DeploymentPreconditionError(
            f"Kubernetes context '{config.kube_context}' is not reachable. "
            "The platform must be destroyed before the local foundation is destroyed."
        )
    if not config.paths.foundation_state_path.is_file():
        raise DeploymentPreconditionError(
            f"local foundation Terraform state is missing: {config.paths.foundation_state_path}. "
            "The platform state depends on the foundation contract; restore or recreate it before tearing "
            "down the local platform."
        )

    log.step("Removing completed Superset init hook jobs if present...")
    for stage in config.context.enabled_stages:
        tools.kubectl.delete(
            "job",
            "superset-init-db",
            namespace=config.context.namespace_for(stage),
            context=config.kube_context,
            kubeconfig=config.paths.kubeconfig_path,
            ignore_not_found=True,
            extra_args=("--wait=true",),
            env=env,
        )

    log.step("Destroying Terraform local stack...")
    tools.terraform.init(platform_dir, env=env)
    tools.terraform.destroy(
        platform_dir,
        var_files=platform_var_files(config),
        variables=platform_destroy_variables(config),
        env=env,
    )

    log.step("Restoring the stock CoreDNS Corefile...")
    tools.kubectl.patch(
        "configmap",
        "coredns",
        json.dumps({"data": {"Corefile": _STOCK_COREFILE}}),
        namespace="kube-system",
        context=config.kube_context,
        kubeconfig=config.paths.kubeconfig_path,
        env=env,
    )

    log.step("Removing legacy unmanaged Helm releases if present...")
    cleanup_legacy_helm_releases(config, tools, env=env)

    # Union: the topology's namespaces plus whatever is still labelled as
    # ours, so a stage removed from the profile is not left behind.
    for namespace in dict.fromkeys((*owned_namespaces(config), *managed_namespaces(config, tools, env=env))):
        log.step(f"Deleting namespace '{namespace}' if it still exists...")
        tools.kubectl.delete(
            "namespace",
            namespace,
            context=config.kube_context,
            kubeconfig=config.paths.kubeconfig_path,
            ignore_not_found=True,
            env=env,
        )

    log.step("Teardown complete. Kind cluster is still running.")
