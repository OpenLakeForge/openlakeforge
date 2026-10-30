"""Offline checks for source-controlled Superset report bundles."""

from __future__ import annotations

import typer
from openlakeforge_domain import inventory_for

from olf.common.log import fail

app = typer.Typer(help="Source-controlled Superset report bundles.")


@app.command("validate")
def validate(
    dashboard: str = typer.Argument(
        "", help="One dashboard bundle under dashboards/superset; defaults to every bundle with a metadata.yaml."
    ),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Check report bundles against the promotion contract: stable identities,
    resolvable references, and no stage-bound or workspace-bound values."""
    from olf.common import report_bundles
    from olf.common.commands._project import writable_project_root

    try:
        root = writable_project_root(project_root)
        declared = {item.name: item.report_source_dir for item in inventory_for(root).dashboards}
        if dashboard and dashboard not in declared:
            raise typer.BadParameter(
                f"{dashboard!r} is not a dashboard bundle (a dashboards/superset directory with a metadata.yaml)"
            )
        selected = (
            [declared[dashboard]]
            if dashboard
            else report_bundles.validate_report_registry(root, tuple(declared.values()))
        )
        errors = report_bundles.validate_report_bundles(root, selected)
    except RuntimeError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for error in errors:
        typer.echo(error)
    if errors:
        raise typer.Exit(code=1)
    typer.echo(f"{len(selected)} report bundle(s) are promotable.")
