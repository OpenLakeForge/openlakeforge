"""Product scaffolding command."""

from __future__ import annotations

import typer

from olf.common.commands._project import writable_project_layout
from olf.common.log import fail
from olf.common.scaffold._commit import commit_plan
from olf.common.scaffold._shared import ScaffoldError, parse_source_resource
from olf.common.scaffold.product import plan_product_new

app = typer.Typer(help="Product-owned Gold scaffolding.")


@app.command("new")
def product_new(
    target: str = typer.Argument(..., help="'<domain>/<product>', e.g. 'hr/headcount'."),
    display_name: str = typer.Option(None, "--display-name", help="Defaults to a title-cased PRODUCT."),
    silver_input: list[str] = typer.Option(
        [], "--silver-input", help="An existing domain Silver table this product consumes. Repeatable."
    ),
    input_: list[str] = typer.Option(
        [],
        "--input",
        help="A new '<source>/<resource>' to validate into the domain's Silver as well as consume. Repeatable.",
    ),
    gold_table: list[str] = typer.Option(..., "--gold-table", help="A Gold mart this product produces. Repeatable."),
    with_report: bool = typer.Option(False, "--with-report", help="Also scaffold a Superset report skeleton."),
    repo_root: str = typer.Option("", "--repo-root", help="Writable project root."),
) -> None:
    """Generate a new Product: a dbt Gold project and a Dagster product
    module, referencing existing domain Silver tables (--silver-input)
    and/or newly-declared ones (--input). Never generates a second dlt
    Bronze loader for a source resource that already exists. Creates the
    domain inline (via --input) when DOMAIN is not declared yet."""
    try:
        layout = writable_project_layout(repo_root)
        project = layout.project
        inputs = tuple(parse_source_resource(value) for value in input_)
        plan = plan_product_new(
            project.root,
            target=target,
            display_name=display_name,
            silver_inputs=tuple(silver_input),
            inputs=inputs,
            gold_tables=tuple(gold_table),
            with_report=with_report,
        )
        commit_plan(project.root, plan, schema_root=project.schema_root)
    except (RuntimeError, ScaffoldError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for line in plan.summary:
        typer.echo(line)
