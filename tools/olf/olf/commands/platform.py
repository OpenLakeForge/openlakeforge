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


@app.command("contract")
def contract(
    profile_file: str = typer.Option(..., "--file", "-f", help="Deployment Profile v1 path."),
) -> None:
    """Print the applied platform's provider contract as JSON.

    It names Secrets and keys, never their values. A job that deploys a
    project revision without the platform's Terraform state points
    OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE at a copy of it (#119).
    """
    import json

    from olf import contracts
    from olf.provider_contracts import ProviderContractError

    context = deployment_context_for_profile(profile_file)
    try:
        # The context's env locates an installed distribution's state under
        # OLF_HOME; the process env alone reads the payload's absent state.
        # Always the applied state: exporting must never echo an earlier copy
        # the override points at (or read the file `>` is truncating).
        payload = contracts.load_provider_contracts(
            str(context.paths.platform_terraform_dir),
            environ=context.command_env(base=os.environ),
            honor_contract_file=False,
        )
    except ProviderContractError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if payload is None:
        raise typer.Exit(code=fail(f"No applied provider contract: run `olf platform apply -f {profile_file}` first."))
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


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
