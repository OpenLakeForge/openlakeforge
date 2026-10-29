"""`olf source new` -- golden-path Bronze source scaffolding."""

from __future__ import annotations

import typer

from olf.commands._project import writable_project_layout
from olf.commands._shared import fail
from olf.scaffold._commit import commit_plan
from olf.scaffold._shared import ScaffoldError
from olf.scaffold.source import plan_source_new

app = typer.Typer(help="Source-owned Bronze ingestion scaffolding.")


@app.command("new")
def source_new(
    source: str = typer.Argument(..., help="Source identifier, e.g. 'marketing_platform'."),
    display_name: str = typer.Option(None, "--display-name", help="Defaults to a title-cased SOURCE."),
    resource: list[str] = typer.Option(..., "--resource", help="A logical resource this source exposes. Repeatable."),
    repo_root: str = typer.Option("", "--repo-root", help="Writable project root."),
) -> None:
    """Generate a new Source: `source.yaml`, a dlt loader, and one placeholder
    example CSV per --resource. The source is discovered from its directory."""
    try:
        layout = writable_project_layout(repo_root)
        project = layout.project
        plan = plan_source_new(project.root, source=source, display_name=display_name, resources=tuple(resource))
        commit_plan(
            project.root, plan, schema_root=project.schema_root, allow_transitional=True
        )
    except (RuntimeError, ScaffoldError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for line in plan.summary:
        typer.echo(line)
