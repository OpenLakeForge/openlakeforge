"""Shared CLI helpers for command groups."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from olf import log


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
        profile = load_deployment_profile(profile_path)
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
    kwargs: dict[str, object] = {
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
        return DeploymentContext.for_provider(resolved_provider, **kwargs)
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
        profile = load_deployment_profile(path)
        topology = resolve_topology(profile)
    except (DeploymentProfileError, OSError) as exc:
        raise typer.Exit(code=fail(f"{path}: {exc}")) from exc
    try:
        selected_stage = StageName(stage) if stage else StageName.DEV
    except ValueError as exc:
        raise typer.Exit(code=fail(f"unknown --stage: {stage!r} (expected dev, uat, or prod)")) from exc
    if topology.stage(selected_stage) is None or not topology.stage(selected_stage).enabled:
        raise typer.Exit(code=fail(f"stage {selected_stage.value!r} is not enabled by {path}."))
    try:
        layout = runtime_layout({**os.environ, "OPENLAKEFORGE_PROJECT_ROOT": str(path.parent)})
    except DistributionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    kwargs: dict[str, object] = {
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
