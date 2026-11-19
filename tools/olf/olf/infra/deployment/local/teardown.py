"""Local platform teardown.

Port of `scripts/local/stack/teardown.sh`. Destroys the Terraform-managed
platform (Helm releases, namespaces) while leaving the kind cluster running;
foundation teardown is a separate step (`olf.infra.deployment.local.foundation`).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from olf.common import log
from olf.common.deployment.errors import DeploymentPreconditionError, ExecutableNotFoundError
from olf.common.deployment.local.config import LocalDeploymentConfig
from olf.common.distribution import default_home
from olf.common.tooling.toolkit import Toolkit
from olf.infra.deployment import kube_ops
from olf.infra.deployment.local.platform import platform_destroy_variables, platform_var_files, restore_stock_corefile

LEGACY_HELM_RELEASES = ("trino", "polaris", "seaweedfs", "garage")
SOURCE_DESTROY_COMMAND = "uv run --project tools/olf --locked olf infra destroy"
INSTALLED_DESTROY_COMMAND = "olf infra destroy"


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
        raise DeploymentPreconditionError(unreachable_context_message(config, tools, env=env))
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
    try:
        tools.terraform.destroy(
            platform_dir,
            var_files=platform_var_files(config),
            variables=platform_destroy_variables(config),
            env=env,
        )
    finally:
        # Even a partial destroy may have deleted the module's Corefile key.
        restore_stock_corefile(config, tools, env=env)

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


def unreachable_context_message(config: LocalDeploymentConfig, tools: Toolkit, *, env: Mapping[str, str]) -> str:
    """Why teardown cannot reach the cluster, naming the state it read.

    The source-tree and installed `olf` keep separate state roots for the same
    kind cluster (#1282), so a teardown run in the mode that did not deploy reads
    the other mode's stale kubeconfig. This does not refresh that kubeconfig from
    kind: the Terraform state beside it belongs to the earlier deployment too, and
    destroying with it would act on the wrong state. It points at the root whose
    kubeconfig matches the running cluster instead.
    """
    paths = config.paths
    kubeconfig = paths.kubeconfig_path
    if paths.installed:
        mode, state_root = "installed olf", paths.state_root
        other_mode, other_kubeconfig, other_command = (
            "source-tree olf",
            paths.repo_root / ".tmp" / "kubeconfigs" / "local.yaml",
            f"{SOURCE_DESTROY_COMMAND} (from the repository root)",
        )
    else:
        mode, state_root = "source-tree olf", paths.work_root
        other_mode, other_kubeconfig, other_command = (
            "installed olf",
            default_home() / "state" / "local" / "kubeconfig.yaml",
            f"{INSTALLED_DESTROY_COMMAND} (the installed olf, from the project directory)",
        )
    lines = [
        f"Kubernetes context '{config.kube_context}' is not reachable through kubeconfig {kubeconfig} "
        f"({mode} state root {state_root}; foundation state {paths.foundation_state_path}). "
        "The platform must be destroyed before the local foundation is destroyed."
    ]

    cluster = config.cluster.name
    try:
        live = tools.kind.get_kubeconfig(cluster, env=env)
    except ExecutableNotFoundError:
        return lines[0]
    if not live.ok:
        lines.append(f"No kind cluster named '{cluster}' is running.")
        return "\n".join(lines)

    live_server = _server(_load(live.stdout), config.kube_context)
    if live_server is None:
        return lines[0]
    read_server = _server(_read(kubeconfig), config.kube_context)
    if read_server != live_server:
        found = f"points at {read_server}" if read_server else "has no server for that context"
        lines.append(
            f"kind cluster '{cluster}' is running at {live_server}, but {kubeconfig} {found}: "
            f"this state belongs to another deployment of '{cluster}'."
        )
    if other_kubeconfig != kubeconfig and _server(_read(other_kubeconfig), config.kube_context) == live_server:
        lines.append(
            f"The {other_mode} kubeconfig {other_kubeconfig} matches the running cluster; "
            f"tear it down with the mode that deployed it: {other_command}."
        )
    return "\n".join(lines)


def _read(path: Path) -> Any:
    try:
        return _load(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _load(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _server(document: Any, context_name: str) -> str | None:
    if not isinstance(document, dict):
        return None
    cluster_name = next(
        (
            entry.get("context", {}).get("cluster")
            for entry in document.get("contexts") or ()
            if isinstance(entry, dict) and entry.get("name") == context_name and isinstance(entry.get("context"), dict)
        ),
        None,
    )
    return next(
        (
            entry["cluster"].get("server")
            for entry in document.get("clusters") or ()
            if isinstance(entry, dict) and entry.get("name") == cluster_name and isinstance(entry.get("cluster"), dict)
        ),
        None,
    )
