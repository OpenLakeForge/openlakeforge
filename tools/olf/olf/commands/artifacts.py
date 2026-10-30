"""Object-storage artifact helpers."""

from __future__ import annotations

import os

import typer

from olf import layers as layers_module
from olf.commands.openmetadata import deploy_openmetadata_metadata
from olf.commands.superset import deploy_superset_reports

app = typer.Typer(help="Object-storage artifact helpers.")


@app.command("deploy-optional-layers")
def artifacts_deploy_optional_layers() -> None:
    """Deploy artifacts for enabled optional platform layers."""
    layers_module.deploy_enabled_artifacts(
        os.environ,
        deploy_reports=deploy_superset_reports,
        deploy_metadata=deploy_openmetadata_metadata,
        report=lambda message: typer.echo(f"==> {message}"),
    )
