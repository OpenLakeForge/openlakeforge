"""Shared CLI helpers for command groups."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from olf import config, log
from olf.deployment.cloud.cluster_name import aws_cluster_name


def log_step(message: str) -> None:
    """Announce a CLI step."""
    log.step(message)


def fail(message: str) -> int:
    """Log error and return exit code 1."""
    log.error(message)
    return 1


def resolve_topology(project_root: Path, *, provider, preset: str = ""):  # noqa: ANN001, ANN202
    """Resolve the run's effective `DeploymentTopology`.

    An explicit `--profile slim|full` selects the deprecated v0.2 shorthand
    (ADR 0011): one enabled DEV stage using that preset's capability
    defaults. With no `--profile`, the project-root Deployment Profile is
    authoritative.

    Two cases fall back to that same single-DEV shorthand rather than to a
    guess: no profile file at all (a directory that is not an OpenLakeForge
    project yet -- `olf check contracts` is what requires one), and a profile
    that targets a different provider than the one requested, which cannot
    describe this deployment. A profile file that exists but is invalid fails
    closed instead: it was written to be used.
    """
    from olf.profile import (
        DeploymentProfileError,
        Preset,
        legacy_single_stage_topology,
        load_deployment_profile,
    )
    from olf.profile import (
        resolve_topology as resolve,
    )

    if preset:
        return legacy_single_stage_topology(provider=provider, preset=Preset(preset))
    profile_path = project_root / "openlakeforge.yaml"
    if not profile_path.is_file():
        return legacy_single_stage_topology(provider=provider, preset=Preset.FULL)
    try:
        from olf.distribution import runtime_layout

        layout = runtime_layout({**os.environ, "OPENLAKEFORGE_PROJECT_ROOT": str(project_root)})
        profile = load_deployment_profile(profile_path, distribution_root=layout.distribution_root)
    except (DeploymentProfileError, OSError) as exc:
        raise typer.Exit(code=fail(f"{profile_path}: {exc}")) from exc
    if profile.provider.type != provider:
        log.warn(
            f"{profile_path} targets provider {profile.provider.type.value!r}; "
            f"--provider {provider.value!r} resolves a single DEV stage instead."
        )
        return legacy_single_stage_topology(provider=provider, preset=profile.preset)
    return resolve(profile)


def deployment_context(
    provider: str,
    *,
    profile: str,
    namespace: str,
    cluster_name: str,
    kubeconfig_path: str = "",
    project_root: str = "",
    stage: str = "",
    allow_stage_removal: bool = False,
    github_connect: bool = False,
):  # noqa: ANN202
    """Resolve the installed-vs-source runtime layout into a `DeploymentContext`.

    Shared by every command that needs provider paths - `olf deploy`/`plan`/
    `doctor`/`destroy`/`status`/`forward` (`olf.commands.deployment`) and
    `olf e2e run` (`olf.commands.e2e`). A second, independent resolution here
    would drift: an installed distribution's Terraform roots, kubeconfig, and
    work/state/cache paths must resolve identically for every command, or
    `olf e2e run` ends up validating a different deployment than `olf deploy`
    just created.
    """
    from olf.deployment.context import DeploymentContext, Profile, Provider
    from olf.deployment.errors import DeploymentPreconditionError
    from olf.distribution import DistributionError, runtime_layout
    from olf.profile import StageName

    try:
        resolved_provider = Provider(provider)
    except ValueError as exc:
        raise typer.Exit(code=fail(f"unknown --provider: {provider!r}")) from exc
    if profile:
        try:
            Profile(profile)
        except ValueError as exc:
            raise typer.Exit(code=fail(f"unknown --profile: {profile!r} (expected 'full' or 'slim')")) from exc
    if namespace:
        # Every stage-aware root (local, azure, aws - #114) derives
        # `olf-system` and `olf-<stage>` from the resolved topology, so an
        # override here would only rename what the artifacts and forwarding
        # paths look for -- never what Terraform creates. Rejecting it beats
        # a platform that applies cleanly while every later phase points at
        # a namespace nobody made.
        raise typer.Exit(
            code=fail(
                f"--namespace is not supported for the {resolved_provider.value} provider: namespaces are "
                "derived from the Deployment Profile (olf-system plus olf-<stage>). Use --stage to select a stage."
            )
        )
    if stage:
        try:
            StageName(stage)
        except ValueError as exc:
            raise typer.Exit(
                code=fail(f"unknown --stage: {stage!r} (expected one of {[s.value for s in StageName]})")
            ) from exc

    layout_env = dict(os.environ)
    if project_root:
        layout_env["OPENLAKEFORGE_PROJECT_ROOT"] = project_root
    try:
        layout = runtime_layout(layout_env)
    except DistributionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc

    topology = resolve_topology(layout.project_root, provider=resolved_provider, preset=profile)
    kwargs: dict[str, Any] = {
        "repo_root": layout.project_root,
        "distribution_root": layout.distribution_root,
        "state_root": None if layout.is_source else layout.state_root,
        "work_root": None if layout.is_source else layout.work_root,
        "cache_root": None if layout.is_source else layout.cache_root,
        # `--profile` names a preset, not a stage set: the resolved topology
        # is what selects capabilities, and this only still picks the
        # platform tfvars file (`TerraformSettings.from_environment`).
        "profile": Profile(topology.preset.value),
        "topology": topology,
        "allow_stage_removal": allow_stage_removal,
    }
    if stage:
        kwargs["stage"] = stage
    if resolved_provider == Provider.LOCAL and cluster_name:
        kwargs["cluster_name"] = cluster_name
    if kubeconfig_path:
        kwargs["kubeconfig_path"] = Path(kubeconfig_path)
    try:
        return replace(DeploymentContext.for_provider(resolved_provider, **kwargs), github_connect=github_connect)
    except DeploymentPreconditionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def deployment_context_for_profile(
    profile_file: str,
    *,
    stage: str = "",
    cluster_name: str = "",
    kubeconfig_path: str = "",
    var_file: str = "",
    allow_stage_removal: bool = False,
):  # noqa: ANN202
    """Resolve a profile file without requiring a writable data project.

    #115 deliberately gives platform and activation commands a profile path:
    a project revision may have been built in another checkout, and platform
    deployment must not inspect that checkout merely to find its topology.
    ``var_file`` is accepted by callers for a uniform command shape; Terraform
    wiring remains in the provider config built from the returned context.
    """
    from olf.deployment.context import DeploymentContext, Profile, Provider
    from olf.deployment.errors import DeploymentPreconditionError
    from olf.distribution import DistributionError, runtime_layout
    from olf.profile import DeploymentProfileError, StageName, load_deployment_profile, resolve_topology

    path = Path(profile_file).resolve()
    try:
        layout = runtime_layout({**os.environ, "OPENLAKEFORGE_PROJECT_ROOT": str(path.parent)})
    except DistributionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    try:
        profile = load_deployment_profile(path, distribution_root=layout.distribution_root)
        topology = resolve_topology(profile)
    except (DeploymentProfileError, OSError) as exc:
        raise typer.Exit(code=fail(f"{path}: {exc}")) from exc
    try:
        selected_stage = StageName(stage) if stage else StageName.DEV
    except ValueError as exc:
        raise typer.Exit(code=fail(f"unknown --stage: {stage!r} (expected dev, uat, or prod)")) from exc
    resolved_stage = topology.stage(selected_stage)
    if resolved_stage is None or not resolved_stage.enabled:
        raise typer.Exit(code=fail(f"stage {selected_stage.value!r} is not enabled by {path}."))
    kwargs: dict[str, Any] = {
        "repo_root": path.parent,
        "distribution_root": layout.distribution_root,
        "state_root": None if layout.is_source else layout.state_root,
        "work_root": None if layout.is_source else layout.work_root,
        "cache_root": None if layout.is_source else layout.cache_root,
        "profile": Profile(topology.preset.value),
        "topology": topology,
        "stage": selected_stage,
        # Reaching a Deployment Profile through `-f` is what marks the #115
        # lifecycle: the platform stops owning user code so `olf project
        # deploy` can own it per stage.
        "manage_user_deployments": False,
        "allow_stage_removal": allow_stage_removal,
    }
    if cluster_name and profile.provider.type == Provider.LOCAL:
        kwargs["cluster_name"] = cluster_name
    if kubeconfig_path:
        kwargs["kubeconfig_path"] = Path(kubeconfig_path)
    try:
        return DeploymentContext.for_provider(profile.provider.type, **kwargs)
    except DeploymentPreconditionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
def resolve_kubeconfig_path(env: str, default: Path) -> Path:
    """Mirror `olf.e2e._runner.configure_kubeconfig`'s override precedence.

    This command exports `KUBECONFIG` via `applied_contract_environment`
    before `e2e.run()` (and thus `_runner.configure_kubeconfig`) ever runs -
    if it only consulted the bare `KUBECONFIG` env var here, it would shadow
    the documented per-provider override (`AWS_KUBECONFIG_PATH`/
    `AZURE_KUBECONFIG_PATH`) with the plain default path before `_runner`
    gets a chance to fall back to it. `default` is the resolved
    `DeploymentContext`'s own kubeconfig path - the same one `olf deploy`
    wrote to, so an installed distribution's `olf e2e run` finds it without
    the caller having to set `KUBECONFIG` by hand.
    """
    provider_override = config.env(f"{env.upper()}_KUBECONFIG_PATH")
    configured = config.env("KUBECONFIG") or provider_override
    if configured:
        return Path(configured)
    return default


def default_kube_context(env: str, foundation_state_path: Path | None = None) -> str:
    """Mirror `olf.e2e._runner`'s own per-environment `KUBE_CONTEXT` fallback.

    Must match exactly: this value is what `e2e.run()` would derive on its
    own from `os.environ.get("KUBE_CONTEXT", ...)` if that variable were
    left unset - but `applied_contract_environment` always sets
    `KUBE_CONTEXT` in `os.environ` (even to a resolved default), so it has
    to resolve the same fallback here to avoid exporting an empty string
    for `aws-e2e`/`azure-e2e`, which don't set `KUBE_CONTEXT` themselves.
    """
    if env == "local":
        cluster_name = config.env("CLUSTER_NAME", "openlakeforge-local")
        return f"kind-{cluster_name}"
    if env == "azure":
        return config.env("AZURE_CLUSTER_NAME", "aks-openlakeforge-poc")
    return aws_cluster_name(os.environ, foundation_state_path)

@contextmanager
def applied_runtime_environment(context, *, env: str) -> Iterator[str]:  # noqa: ANN001
    """Apply the stage's provider-contract environment and yield its kube context.

    What `olf e2e run` and `olf query` both need before talking to the
    cluster: the in-process equivalent of sourcing the contract exports, with
    the same installed-vs-source path resolution `olf deploy` used.
    """
    from olf.deployment import contract_env

    contract_dir = Path(config.env("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(context.paths.platform_terraform_dir)))
    kube_context = config.env("KUBE_CONTEXT") or default_kube_context(env, context.paths.foundation_state_path)
    # Fresh contract readers create their own managed-tool resolver and, for
    # an installed distribution, must resolve the same external Terraform
    # state/data roots `olf deploy` wrote to (`DeploymentContext.command_env`,
    # `olf.tooling.terraform.external_state_options`) - without this the
    # contract read silently targets the payload's absent default state.
    with contract_env.applied_contract_environment(
        contract_terraform_dir=contract_dir,
        repo_root=context.paths.repo_root,
        namespace=context.namespace,
        kube_context=kube_context,
        kubeconfig_path=resolve_kubeconfig_path(env, context.paths.kubeconfig_path),
        port_forward_log_prefix=Path(
            config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", str(context.paths.port_forward_log_prefix))
        ),
        environ=context.command_env(base=os.environ),
        topology=context.topology,
        stage=context.stage,
    ):
        yield kube_context
