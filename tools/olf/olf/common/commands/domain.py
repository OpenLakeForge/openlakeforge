"""`olf domain new` -- golden-path domain (Silver) scaffolding."""

from __future__ import annotations

import typer

from olf.common.commands._project import writable_project_layout
from olf.common.log import fail
from olf.common.scaffold._commit import commit_plan
from olf.common.scaffold._shared import ScaffoldError, parse_source_resource
from olf.common.scaffold.domain import plan_domain_new

app = typer.Typer(help="Domain-owned Silver scaffolding, and the domains a deployment governs.")


@app.command("new")
def domain_new(
    domain: str = typer.Argument(..., help="Domain identifier, e.g. 'hr'."),
    display_name: str = typer.Option(None, "--display-name", help="Defaults to a title-cased DOMAIN."),
    input_: list[str] = typer.Option(
        ..., "--input", help="An existing '<source>/<resource>' this domain validates into Silver. Repeatable."
    ),
    repo_root: str = typer.Option("", "--repo-root", help="Writable project root."),
) -> None:
    """Generate a new Domain: a Silver package plus a Floe Bronze-to-Silver
    contract with one entity per --input. Adds DOMAIN to governance.yml
    with no products -- a domain may be created and validated before any
    product consumes its Silver tables."""
    try:
        layout = writable_project_layout(repo_root)
        project = layout.project
        inputs = tuple(parse_source_resource(value) for value in input_)
        plan = plan_domain_new(project.root, domain=domain, display_name=display_name, inputs=inputs)
        commit_plan(
            project.root, plan, schema_root=project.schema_root, allow_transitional=True
        )
    except (RuntimeError, ScaffoldError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for line in plan.summary:
        typer.echo(line)
