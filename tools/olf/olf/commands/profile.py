"""Commands for validating and resolving the OpenLakeForge Deployment Profile."""

from __future__ import annotations

from pathlib import Path

import typer

from olf.profile import DeploymentProfile, DeploymentProfileError, load_deployment_profile, resolve_topology
from olf.project import ProjectSpec

app = typer.Typer(help="Validate and resolve the OpenLakeForge Deployment Profile.")


def _load(project: str) -> DeploymentProfile:
    from olf.distribution import runtime_layout

    layout = runtime_layout()
    spec = ProjectSpec(root=Path(project), distribution_root=layout.distribution_root)
    return load_deployment_profile(spec.profile_path, distribution_root=spec.distribution_root)


@app.command("validate")
def validate(
    project: str = typer.Option(..., "--project", help="Explicit writable project root."),
) -> None:
    """Validate the Deployment Profile at `<project>/openlakeforge.yaml`."""
    try:
        _load(project)
    except (DeploymentProfileError, OSError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo("Deployment Profile is valid.")


@app.command("resolve")
def resolve(
    project: str = typer.Option(..., "--project", help="Explicit writable project root."),
    json_output: bool = typer.Option(False, "--json", help="Emit the resolved topology as JSON."),
) -> None:
    """Resolve the Deployment Profile into one effective `DeploymentTopology`."""
    try:
        profile = _load(project)
    except (DeploymentProfileError, OSError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    topology = resolve_topology(profile)
    if json_output:
        typer.echo(topology.render_json())
        return

    typer.echo(f"provider={topology.provider.value} region={topology.region} preset={topology.preset.value}")
    for stage in topology.stages:
        typer.echo(
            f"  {stage.name.value}: enabled={stage.enabled} "
            f"analytics={stage.capabilities.analytics} governance={stage.capabilities.governance}"
        )
