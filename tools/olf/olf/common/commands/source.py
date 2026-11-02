"""`olf source new` -- golden-path Bronze source scaffolding."""

from __future__ import annotations

import typer

from olf.common.commands._project import writable_project_layout
from olf.common.log import fail
from olf.common.scaffold._commit import commit_plan
from olf.common.scaffold._shared import ScaffoldError
from olf.common.scaffold.source import plan_source_new

app = typer.Typer(help="Source-owned Bronze ingestion scaffolding.")


@app.command("new")
def source_new(
    source: str = typer.Argument(..., help="Source identifier, e.g. 'marketing_platform'."),
    display_name: str = typer.Option(None, "--display-name", help="Defaults to a title-cased SOURCE."),
    resource: list[str] = typer.Option(..., "--resource", help="A logical resource this source exposes. Repeatable."),
    kind: str = typer.Option(
        "csv",
        "--kind",
        help="csv: example CSV files loaded in full; api or sql: an incremental dlt connector.",
    ),
    cursor: str = typer.Option(
        None,
        "--cursor",
        help="api/sql: the increasing column each resource is read past (api: updated_at, sql: --primary-key).",
    ),
    primary_key: str = typer.Option("id", "--primary-key", help="api/sql: the column Silver merges each resource on."),
    repo_root: str = typer.Option("", "--repo-root", help="Writable project root."),
) -> None:
    """Generate a new Source: `source.yaml`, a dlt loader, and one example CSV
    per --resource. The source is discovered from its directory."""
    try:
        layout = writable_project_layout(repo_root)
        project = layout.project
        plan = plan_source_new(
            project.root,
            source=source,
            display_name=display_name,
            resources=tuple(resource),
            kind=kind,
            cursor=cursor,
            primary_key=primary_key,
        )
        commit_plan(project.root, plan, schema_root=project.schema_root, allow_transitional=True)
    except (RuntimeError, ScaffoldError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for line in plan.summary:
        typer.echo(line)
