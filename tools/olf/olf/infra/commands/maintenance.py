"""`olf infra maintenance run` -- launch an Iceberg maintenance job and report its per-table results."""

from __future__ import annotations

import contextlib
import json
import sys

import typer

from olf.common.log import fail
from olf.infra.commands._shared import applied_runtime_environment, deployment_context_for_profile

app = typer.Typer(help="Run Iceberg maintenance: snapshot expiry and orphan-file cleanup.")


@app.command("run")
def maintenance_run(
    layer: str = typer.Option(..., "--layer", help="Layer to maintain: silver (Floe, no Trino) or gold (Trino)."),
    stage: str = typer.Option("", "--stage", help="Stage to run in: dev, uat, or prod. Defaults to dev."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be expired; delete nothing."),
    json_output: bool = typer.Option(
        False, "--json", help="Print the report as JSON on stdout; progress goes to stderr."
    ),
    profile_file: str = typer.Option(
        "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile the deployment was applied from."
    ),
) -> None:
    """Launch a stage's maintenance job, wait for it, and print the per-table results.

    The jobs exist on every stage whatever the state of their weekly schedules, so a run
    here does not depend on a schedule being enabled. Retention is the job's default
    (7 days, 10 snapshots); the engines refuse less. Expiring a snapshot ends time travel to it.
    """
    from olf.common.deployment.errors import DeploymentError
    from olf.common.provider_contracts import ProviderContractError
    from olf.infra import maintenance
    from olf.infra.e2e._runner import configure_kubeconfig, prepare_config, prepare_kube_context
    from olf.infra.e2e._shell import E2EError

    if layer not in maintenance.LAYERS:
        raise typer.Exit(code=fail(f"unknown --layer {layer!r}; expected silver or gold."))
    context = deployment_context_for_profile(profile_file, stage=stage)
    provider = context.provider.value
    # Launching and polling print progress on stdout; --json keeps stdout to the report.
    progress = contextlib.redirect_stdout(sys.stderr) if json_output else contextlib.nullcontext()
    try:
        with progress, applied_runtime_environment(context, env=provider) as contract:
            cfg = prepare_config(
                provider,
                suite=None,
                namespace=context.namespace,
                shared_namespace=context.shared_namespace,
                kube_context=contract.kube_context,
                repo_root=context.paths.repo_root,
                distribution_root=context.paths.distribution_root,
                environ=contract,
            )
            prepare_kube_context(cfg, configure_kubeconfig(cfg))
            report = maintenance.run_maintenance(cfg, layer=layer, stage=context.stage.value, dry_run=dry_run)
    except (E2EError, DeploymentError, ProviderContractError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(json.dumps(report.as_dict(), indent=2) if json_output else maintenance.render(report))
