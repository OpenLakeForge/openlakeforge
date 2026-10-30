"""Cloud (AWS/Azure) foundation Terraform lifecycle.

Port of `scripts/{aws,azure}/foundation/{up,down}.sh`. Terraform remains the
owner of the EKS/AKS cluster and registry - this module only orchestrates
Terraform/kubectl/aws-or-az calls through `CloudBackend`, mirroring
`olf.deployment.local.foundation` for the local kind cluster.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from olf import log
from olf.deployment.cloud.backend import CloudBackend, FoundationFacts
from olf.deployment.cloud.config import CloudDeploymentConfig
from olf.deployment.errors import DeploymentPreconditionError
from olf.tooling.kubectl import KubeContextUnreachableError
from olf.tooling.toolkit import Toolkit


def _resolve_foundation_tfvars_file(
    config: CloudDeploymentConfig, backend: CloudBackend, environ: Mapping[str, str], foundation_dir: Path
) -> Path | None:
    """Honor an explicit `--var-file` CLI override for foundation operations.

    Checked in order:
    1. `foundation_var_file` - an explicit CLI `--var-file`, for either
       provider. Never falls through to `config.terraform.var_file`: that
       field is the *platform* apply's var-file, auto-discovered from the
       platform Terraform root's own `sandbox.tfvars` (AWS) - a file that
       can legitimately differ from the foundation root's `sandbox.tfvars`
       when both exist side by side (the documented AWS setup instructs
       exactly this). Falling through to it would silently apply the
       platform's tags/settings to the foundation, or - for Azure - the
       ADR-0027-forbidden platform var-file into the foundation apply.
    2. The backend's own `AWS_TFVARS_FILE`/`AZURE_TFVARS_FILE` resolution,
       scoped to the foundation root, when no explicit override was given.
    """
    if config.terraform.foundation_var_file is not None:
        return config.terraform.foundation_var_file
    return backend.foundation_tfvars_file(
        environ, repo_root=config.paths.repo_root, foundation_terraform_dir=foundation_dir
    )


def foundation_up(
    config: CloudDeploymentConfig,
    tools: Toolkit,
    backend: CloudBackend,
    *,
    environ: Mapping[str, str],
    env: Mapping[str, str],
) -> FoundationFacts:
    foundation_dir = config.paths.foundation_terraform_dir
    config.context.prepare_directories()

    log.step(f"Checking {backend.scope} foundation prerequisites...")
    backend.preflight(tools, env=env)

    tfvars_file = _resolve_foundation_tfvars_file(config, backend, environ, foundation_dir)
    var_files = (str(tfvars_file),) if tfvars_file is not None else ()

    log.step(f"Initializing Terraform {backend.scope} foundation...")
    tools.terraform.init(foundation_dir, env=env)

    log.step(f"Applying Terraform {backend.scope} foundation...")
    tools.terraform.apply(
        foundation_dir,
        var_files=var_files,
        variables=backend.foundation_apply_variables(config, environ),
        env=env,
    )

    facts = backend.resolve_foundation_facts(tools, foundation_terraform_dir=foundation_dir, env=env)

    log.step(f"Fetching {backend.scope} kube credentials...")
    config.paths.kubeconfig_path.parent.mkdir(parents=True, exist_ok=True)
    backend.update_kubeconfig(tools, facts, kubeconfig_path=config.paths.kubeconfig_path, env=env)

    try:
        tools.kubectl.require_context_reachable(facts.kube_context, kubeconfig=config.paths.kubeconfig_path, env=env)
    except KubeContextUnreachableError as exc:
        raise DeploymentPreconditionError(
            f"Kubernetes context '{facts.kube_context}' is not reachable after applying the foundation: {exc}"
        ) from exc

    log.step(f"{backend.scope.capitalize()} foundation is ready. Kubernetes context: {facts.kube_context}")
    return facts


def foundation_down(
    config: CloudDeploymentConfig,
    tools: Toolkit,
    backend: CloudBackend,
    *,
    environ: Mapping[str, str],
    env: Mapping[str, str],
    force: bool = False,
) -> None:
    foundation_dir = config.paths.foundation_terraform_dir

    log.step(f"Initializing Terraform {backend.scope} foundation...")
    tools.terraform.init(foundation_dir, env=env)

    state_result = tools.terraform.state_show(
        foundation_dir, backend.foundation_state_resource_addr(), env=env, check=False
    )
    if not state_result.ok:
        log.step(f"No {backend.scope} foundation state exists.")
        return

    # Deliberately checked after (not before) state existence: the removed
    # teardown scripts never required a cloud login just to reach this
    # already-clean no-op, so a cleanup run against expired credentials must
    # not fail authentication before it can even see there's nothing to do.
    log.step(f"Checking {backend.scope} foundation prerequisites...")
    backend.preflight(tools, env=env)

    tfvars_file = _resolve_foundation_tfvars_file(config, backend, environ, foundation_dir)
    var_files = (str(tfvars_file),) if tfvars_file is not None else ()

    facts = backend.resolve_foundation_facts(tools, foundation_terraform_dir=foundation_dir, env=env)

    if backend.cluster_reachable(tools, facts, env=env):
        config.paths.kubeconfig_path.parent.mkdir(parents=True, exist_ok=True)
        backend.update_kubeconfig(tools, facts, kubeconfig_path=config.paths.kubeconfig_path, env=env)

    # Deliberately independent of cluster_reachable(): that cloud API probe
    # can fail transiently (or on a permissions gap) without the cluster
    # actually being gone. The removed teardown scripts ran this kubectl
    # check unconditionally, so a failed/skipped kubeconfig refresh must not
    # silently authorize destroying a foundation with platform resources
    # still present - only kubectl itself failing to reach the namespace
    # (e.g. because the cluster is genuinely gone) waives the guard.
    if not force:
        namespace_result = tools.kubectl.get(
            "namespace",
            name=config.namespace,
            context=facts.kube_context,
            kubeconfig=config.paths.kubeconfig_path,
            env=env,
            check=False,
        )
        if namespace_result.ok:
            raise DeploymentPreconditionError(
                f"namespace '{config.namespace}' still exists on '{facts.kube_context}'. Run "
                f"'olf destroy --provider {backend.scope} --phase platform' before destroying the "
                f"{backend.scope} foundation. Pass --force only if you intentionally want to delete "
                "the foundation with platform resources still present."
            )

    log.step(f"Destroying Terraform {backend.scope} foundation...")
    tools.terraform.destroy(
        foundation_dir,
        var_files=var_files,
        variables=backend.foundation_destroy_variables(config, environ),
        env=env,
    )
    log.step(f"{backend.scope.capitalize()} foundation is destroyed.")


def require_foundation_facts(
    config: CloudDeploymentConfig, tools: Toolkit, backend: CloudBackend, *, env: Mapping[str, str]
) -> FoundationFacts:
    """Resolve foundation facts for a phase that requires the foundation to already exist.

    Port of `prepare_eks_context`/`prepare_aks_context`'s precondition
    checks in `platform-up.sh`/`deploy-artifacts.sh`/`teardown.sh`: the
    foundation state file must exist, and the resulting Kubernetes context
    must be reachable, before any platform-level Terraform or kubectl call
    runs. Used by every phase after foundation - platform, artifacts,
    teardown, status, forward.
    """
    if not config.paths.foundation_state_path.is_file():
        raise DeploymentPreconditionError(
            f"{backend.scope} foundation Terraform state is missing: {config.paths.foundation_state_path}. "
            f"Run the foundation phase before applying the {backend.scope} platform."
        )

    facts = backend.resolve_foundation_facts(
        tools, foundation_terraform_dir=config.paths.foundation_terraform_dir, env=env
    )

    config.paths.kubeconfig_path.parent.mkdir(parents=True, exist_ok=True)
    backend.update_kubeconfig(tools, facts, kubeconfig_path=config.paths.kubeconfig_path, env=env)

    try:
        tools.kubectl.require_context_reachable(facts.kube_context, kubeconfig=config.paths.kubeconfig_path, env=env)
    except KubeContextUnreachableError as exc:
        raise DeploymentPreconditionError(
            f"Kubernetes context '{facts.kube_context}' is not reachable. "
            f"Run the foundation phase before applying the {backend.scope} platform."
        ) from exc

    return facts
