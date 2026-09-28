"""Profile-driven static platform commands (#115)."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from olf.commands._shared import deployment_context_for_profile, fail

app = typer.Typer(help="Plan and apply static foundation/platform infrastructure.")


def _engine(context, *, var_file: str):  # noqa: ANN001, ANN202
    from olf.deployment.engine import DeploymentEngine, Toolkit, build_provider
    from olf.deployment.errors import DeploymentError

    try:
        env = context.command_env(base=os.environ)
        return DeploymentEngine(
            build_provider(
                context,
                toolkit=Toolkit.default(environ=env),
                environ=env,
                var_file=Path(var_file) if var_file else None,
            )
        )
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def _phase(value: str):  # noqa: ANN202
    from olf.deployment.engine import DeploymentPhase

    if value not in {"all", "foundation", "platform"}:
        raise typer.Exit(code=fail("--phase must be all, foundation, or platform."))
    return DeploymentPhase(value)


@app.command("plan")
def plan(
    profile_file: str = typer.Option(..., "--file", "-f", help="Deployment Profile v1 path."),
    phase: str = typer.Option("all", "--phase", help="all, foundation, or platform."),
    var_file: str = typer.Option("", "--var-file", help="Provider-specific Terraform tfvars override."),
    detailed_exitcode: bool = typer.Option(False, "--detailed-exitcode", help="Return 2 when changes are pending."),
) -> None:
    """Plan only Terraform-owned lifecycle phases for a Deployment Profile."""
    from olf.deployment.errors import DeploymentError

    context = deployment_context_for_profile(profile_file)
    try:
        changes = _engine(context, var_file=var_file).plan(_phase(phase))
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("Terraform changes are pending." if changes else "Terraform reports no changes.")
    if changes and detailed_exitcode:
        raise typer.Exit(code=2)


@app.command("apply")
def apply(
    profile_file: str = typer.Option(..., "--file", "-f", help="Deployment Profile v1 path."),
    phase: str = typer.Option("all", "--phase", help="all, foundation, or platform."),
    var_file: str = typer.Option("", "--var-file", help="Provider-specific Terraform tfvars override."),
    allow_stage_removal: bool = typer.Option(
        False,
        "--allow-stage-removal",
        help=(
            "Permit an apply that removes an already-applied stage, or that replaces the pre-v0.3 shared "
            "namespace ('lakehouse') with the current one, destroying its SeaweedFS/PostgreSQL/Polaris state."
        ),
    ),
) -> None:
    """Apply foundation, local image prefetch, and platform without project artifacts."""
    from olf.deployment.engine import DeploymentPhase
    from olf.deployment.errors import DeploymentError

    context = deployment_context_for_profile(profile_file, allow_stage_removal=allow_stage_removal)
    engine = _engine(context, var_file=var_file)
    selected = _phase(phase)
    try:
        if selected is DeploymentPhase.ALL:
            for item in (DeploymentPhase.FOUNDATION, DeploymentPhase.PREFETCH, DeploymentPhase.PLATFORM):
                engine.deploy(item)
        else:
            engine.deploy(selected)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
