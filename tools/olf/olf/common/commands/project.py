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


@app.command("migrate")
def migrate(
    repo_root: str = typer.Option("", "--repo-root", help="Writable project root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff and change nothing."),
) -> None:
    """Rewrite the project's descriptors to the current API versions.

    governance.yml: v1beta1 -> v1beta2, nesting each root product under its
    domain. Refuses, leaving every file untouched, on an unknown domain, a
    duplicate id, or a layout it cannot rewrite safely."""
    import difflib
    import os

    from olf.common.commands._project import writable_project_root
    from olf.common.governance_migration import LEGACY_API_VERSION, MigrationError, migrate_governance_text

    try:
        path = writable_project_root(repo_root) / "lakehouse_code" / "governance.yml"
    except RuntimeError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if not path.is_file():
        typer.echo("Nothing to migrate: no lakehouse_code/governance.yml.")
        return
    original = path.read_text(encoding="utf-8")
    if LEGACY_API_VERSION not in original:
        typer.echo("lakehouse_code/governance.yml is already current.")
        return
    try:
        migrated = migrate_governance_text(original, source="lakehouse_code/governance.yml")
    except MigrationError as exc:
        raise typer.Exit(code=fail(f"{exc}\nNo file was changed.")) from exc
    typer.echo(
        "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                migrated.splitlines(keepends=True),
                "a/lakehouse_code/governance.yml",
                "b/lakehouse_code/governance.yml",
            )
        ),
        nl=False,
    )
    if dry_run:
        return
    staged = path.with_name(path.name + ".migrating")
    staged.write_text(migrated, encoding="utf-8")
    os.replace(staged, path)
    typer.echo("Migrated lakehouse_code/governance.yml to openlakeforge.io/v1beta2.")


@app.command("graph")
def graph(
    project: str = typer.Option(..., "--project", help="Explicit writable project root."),
) -> None:
    """Print the Source -> Domain -> Product -> Dashboard lineage as a Mermaid flowchart."""
    from openlakeforge_core import LakehouseDescriptorError, load_lakehouse_inventory

    from olf.common.lineage_graph import render_lineage_graph

    try:
        inventory = load_lakehouse_inventory(project)
    except LakehouseDescriptorError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(render_lineage_graph(inventory), nl=False)
