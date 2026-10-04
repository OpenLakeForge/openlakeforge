"""Thin `olf deploy`/`destroy`/`status`/`forward` commands.

Each command builds a `DeploymentContext` + `DeploymentEngine` and delegates
immediately; all lifecycle logic lives in `olf.deployment.*`. The `--phase`
option is what lets the granular Make target delegates (`local-foundation-up`,
`local-platform-up`, ...) share this exact code path instead of duplicating
lifecycle logic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from olf.commands._shared import deployment_context as _build_context
from olf.commands._shared import fail

app_help = "Deployment lifecycle: foundation, platform, and artifact orchestration."


def _build_engine(context, *, var_file: str):  # noqa: ANN001, ANN202
    from olf.deployment.engine import DeploymentEngine, Toolkit, build_provider
    from olf.deployment.errors import DeploymentError

    try:
        tooling_env = context.command_env(base=os.environ)
        provider = build_provider(
            context,
            toolkit=Toolkit.default(environ=tooling_env),
            environ=tooling_env,
            var_file=Path(var_file) if var_file else None,
        )
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    return DeploymentEngine(provider)


def _resolve_phase(phase: str, *, valid: tuple[str, ...]):  # noqa: ANN202
    from olf.deployment.engine import DeploymentPhase

    if phase not in valid:
        raise typer.Exit(code=fail(f"unknown --phase: {phase!r} (expected one of {', '.join(valid)})"))
    return DeploymentPhase(phase)


def deploy(
    provider: str = typer.Option("local", "--provider", help="Target deployment provider."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    phase: str = typer.Option(
        "all", "--phase", help="'all', 'foundation', 'prefetch', 'platform', or 'artifacts'."
    ),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option("", "--stage", help="Stage to select: dev, uat, or prod. Defaults to dev."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    var_file: str = typer.Option("", "--var-file", help="Terraform tfvars file override."),
    allow_stage_removal: bool = typer.Option(
        False,
        "--allow-stage-removal",
        help=(
            "Permit an apply that removes an already-applied stage, or that replaces the pre-v0.3 shared "
            "namespace ('lakehouse') with the current one, destroying its SeaweedFS/PostgreSQL/Polaris state."
        ),
    ),
) -> None:
    """Deploy a provider's lifecycle, or a single phase of it."""
    from olf.deployment.errors import DeploymentError

    context = _build_context(
        provider,
        profile=profile,
        namespace=namespace,
        stage=stage,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
        allow_stage_removal=allow_stage_removal,
    )
    engine = _build_engine(context, var_file=var_file)
    resolved_phase = _resolve_phase(phase, valid=("all", "foundation", "prefetch", "platform", "artifacts"))
    try:
        engine.deploy(resolved_phase)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def plan(
    provider: str = typer.Option("local", "--provider", help="Target deployment provider."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    phase: str = typer.Option("all", "--phase", help="'all', 'foundation', or 'platform'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option("", "--stage", help="Stage to select: dev, uat, or prod. Defaults to dev."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    var_file: str = typer.Option("", "--var-file", help="Terraform tfvars file override."),
    allow_stage_removal: bool = typer.Option(
        False,
        "--allow-stage-removal",
        help=(
            "Permit an apply that removes an already-applied stage, or that replaces the pre-v0.3 shared "
            "namespace ('lakehouse') with the current one, destroying its SeaweedFS/PostgreSQL/Polaris state."
        ),
    ),
    detailed_exitcode: bool = typer.Option(False, "--detailed-exitcode", help="Return 2 when changes are pending."),
) -> None:
    """Plan Terraform-managed deployment phases without applying them."""
    from olf.deployment.errors import DeploymentError

    context = _build_context(
        provider,
        profile=profile,
        namespace=namespace,
        stage=stage,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
        allow_stage_removal=allow_stage_removal,
    )
    engine = _build_engine(context, var_file=var_file)
    resolved_phase = _resolve_phase(phase, valid=("all", "foundation", "platform"))
    try:
        changes = engine.plan(resolved_phase)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("Terraform changes are pending." if changes else "Terraform reports no changes.")
    if changes and detailed_exitcode:
        raise typer.Exit(code=2)


def doctor(
    provider: str = typer.Option("local", "--provider", help="Target deployment provider."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    phase: str = typer.Option("all", "--phase", help="'all', 'foundation', 'platform', or 'artifacts'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option("", "--stage", help="Stage to select: dev, uat, or prod. Defaults to dev."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    var_file: str = typer.Option("", "--var-file", help="Terraform tfvars file override."),
) -> None:
    """Check host tools, source inputs, and provider authentication without mutating deployment state."""
    from olf.deployment.errors import DeploymentError

    context = _build_context(
        provider,
        profile=profile,
        namespace=namespace,
        stage=stage,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    engine = _build_engine(context, var_file=var_file)
    resolved_phase = _resolve_phase(phase, valid=("all", "foundation", "platform", "artifacts"))
    try:
        report = engine.doctor(resolved_phase)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(code=1)


def destroy(
    provider: str = typer.Option("local", "--provider", help="Target deployment provider."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    phase: str = typer.Option("all", "--phase", help="'all', 'platform', or 'foundation'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option("", "--stage", help="Stage to select: dev, uat, or prod. Defaults to dev."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    var_file: str = typer.Option("", "--var-file", help="Terraform tfvars file override."),
    force: bool = typer.Option(
        False, "--force", help="Destroy the foundation even if platform resources remain."
    ),
) -> None:
    """Tear down a provider's lifecycle, or a single phase of it."""
    from olf.deployment.errors import DeploymentError

    context = _build_context(
        provider,
        profile=profile,
        namespace=namespace,
        stage=stage,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    engine = _build_engine(context, var_file=var_file)
    resolved_phase = _resolve_phase(phase, valid=("all", "platform", "foundation"))
    try:
        engine.destroy(resolved_phase, force=force)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def status(
    provider: str = typer.Option("local", "--provider", help="Target deployment provider."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option("", "--stage", help="Stage to select: dev, uat, or prod. Defaults to dev."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit service URLs and resource status as JSON."),
) -> None:
    """Print service URLs and pod/service/PVC status for the deployed namespaces."""
    from olf.deployment.errors import DeploymentError

    context = _build_context(
        provider,
        profile="",
        namespace=namespace,
        stage=stage,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    engine = _build_engine(context, var_file="")
    try:
        report = engine.status()
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(json.dumps(report.as_dict(), indent=2) if json_output else report.render())


def forward(
    provider: str = typer.Option("local", "--provider", help="Target deployment provider."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    stage: str = typer.Option("", "--stage", help="Stage to select: dev, uat, or prod. Defaults to dev."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Start port-forwards for the deployed services (Ctrl-C to stop all)."""
    from olf.deployment.errors import DeploymentError

    context = _build_context(
        provider,
        profile=profile,
        namespace=namespace,
        stage=stage,
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    engine = _build_engine(context, var_file="")
    try:
        engine.forward()
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
