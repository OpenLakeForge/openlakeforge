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
    domain. bronze/*/source.yaml: v1alpha3 -> v1beta1. openlakeforge.yaml:
    v1alpha1 -> v1beta1, renaming spec.access.issuer to tls. Refuses, leaving every
    file untouched, if any descriptor cannot be migrated safely."""
    import difflib
    import os

    from olf.common.commands._project import writable_project_root
    from olf.common.descriptor_migration import MigrationError, plan_project_migration

    try:
        root = writable_project_root(repo_root)
        changes = plan_project_migration(root)
    except RuntimeError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    except MigrationError as exc:
        raise typer.Exit(code=fail(f"{exc}\nNo file was changed.")) from exc
    if not changes:
        typer.echo("Descriptors are already current; nothing to migrate.")
        return
    for change in changes:
        relative = change.path.relative_to(root).as_posix()
        typer.echo(
            "".join(
                difflib.unified_diff(
                    change.original.splitlines(keepends=True),
                    change.migrated.splitlines(keepends=True),
                    f"a/{relative}",
                    f"b/{relative}",
                )
            ),
            nl=False,
        )
    if dry_run:
        return
    staged = [(change.path.with_name(change.path.name + ".migrating"), change) for change in changes]
    for temporary, change in staged:
        temporary.write_text(change.migrated, encoding="utf-8")
    for temporary, change in staged:
        os.replace(temporary, change.path)
    typer.echo(f"Migrated {len(changes)} descriptor(s) to the current API versions.")


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
