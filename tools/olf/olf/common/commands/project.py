"""Offline commands for validating a writable OpenLakeForge data project."""

from __future__ import annotations

from pathlib import Path

import typer

from olf.common.log import fail
from olf.common.project import ProjectSpec, validate_project

app = typer.Typer(help="Validate writable OpenLakeForge data projects.")


@app.command("validate")
def validate(
    project: str = typer.Option(..., "--project", help="Explicit writable project root."),
) -> None:
    """Emit a machine-readable report for one project root."""
    from olf.common.distribution import runtime_layout

    layout = runtime_layout()
    report = validate_project(ProjectSpec(root=Path(project), distribution_root=layout.distribution_root))
    typer.echo(report.render_json())
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("graph")
def graph(
    project: str = typer.Option(..., "--project", help="Explicit writable project root."),
) -> None:
    """Print the Source -> Domain -> Product -> Dashboard lineage as a Mermaid flowchart."""
    from openlakeforge_domain import LakehouseDescriptorError, load_lakehouse_inventory

    from olf.common.lineage_graph import render_lineage_graph

    try:
        inventory = load_lakehouse_inventory(project)
    except LakehouseDescriptorError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(render_lineage_graph(inventory), nl=False)
