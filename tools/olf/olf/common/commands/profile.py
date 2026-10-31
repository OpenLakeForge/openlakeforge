"""Commands for validating and resolving the OpenLakeForge Deployment Profile."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from olf.common.profile import (
    DeploymentProfile,
    DeploymentProfileError,
    load_deployment_profile,
    resolve_project,
    resolve_topology,
)
from olf.common.project import ProjectSpec

app = typer.Typer(help="Validate and resolve the OpenLakeForge Deployment Profile.")


def _load(project: str) -> DeploymentProfile:
    from olf.common.distribution import runtime_layout

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
        resolved_project = resolve_project(profile, os.environ)
    except (DeploymentProfileError, OSError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    topology = resolve_topology(profile)
    if json_output:
        typer.echo(json.dumps(json.loads(topology.render_json()) | {"project": vars(resolved_project)}, sort_keys=True))
        return

    typer.echo(f"provider={topology.provider.value} region={topology.region} preset={topology.preset.value}")
    typer.echo(f"sizes: query={topology.query_size.value} storage={topology.storage_size.value}")
    for stage in topology.stages:
        typer.echo(
            f"  {stage.name.value}: enabled={stage.enabled} "
            f"analytics={stage.capabilities.analytics} governance={stage.capabilities.governance}"
            + (f" source_egress={','.join(map(str, stage.source_egress_ports))}" if stage.source_egress_ports else "")
        )
    typer.echo(
        f"project: source={resolved_project.source} image={resolved_project.image_repository or 'provider registry'} "
        f"pull_secret={resolved_project.pull_secret_name}"
    )
