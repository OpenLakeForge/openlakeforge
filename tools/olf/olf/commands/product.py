"""`olf product new` -- golden-path product (Gold) scaffolding; `olf product run` -- launch its pipeline."""

from __future__ import annotations

import typer

from olf.commands._project import writable_project_layout
from olf.commands._shared import applied_runtime_environment, deployment_context_for_profile, fail
from olf.scaffold._commit import commit_plan
from olf.scaffold._shared import ScaffoldError, parse_source_resource
from olf.scaffold.product import plan_product_new

app = typer.Typer(help="Product-owned Gold scaffolding and pipeline runs.")


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


@app.command("run")
def product_run(
    product: str = typer.Argument(..., help="The product whose pipeline to run, e.g. 'order_revenue'."),
    stage: str = typer.Option("", "--stage", help="Stage to run in: dev, uat, or prod. Defaults to dev."),
    full_refresh: str = typer.Option(
        "",
        "--full-refresh",
        help="'bronze' reloads Bronze from scratch and rebuilds Gold; 'gold' rebuilds Gold only. "
        "Omit for an incremental run.",
    ),
    profile_file: str = typer.Option(
        "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile the deployment was applied from."
    ),
) -> None:
    """Launch a product's pipeline in a stage and wait for it to finish.

    Incremental by default: each source extracts past its cursors, Silver
    merges the new rows and dbt runs its incremental models. A full refresh is
    scoped to this one run; the next run without it is incremental again.
    """
    from openlakeforge_domain import FULL_REFRESH_SCOPES, FULL_REFRESH_TAG

    from olf.deployment.errors import DeploymentError
    from olf.e2e._dagster import launch_and_poll_dagster_jobs
    from olf.e2e._runner import prepare_config, prepare_kube_context
    from olf.e2e._shell import E2EError
    from olf.provider_contracts import ProviderContractError

    if full_refresh and full_refresh not in FULL_REFRESH_SCOPES:
        expected = " or ".join(FULL_REFRESH_SCOPES)
        raise typer.Exit(code=fail(f"unknown --full-refresh {full_refresh!r}; expected {expected}."))
    context = deployment_context_for_profile(profile_file, stage=stage)
    provider = context.provider.value
    try:
        with applied_runtime_environment(context, env=provider) as kube_context:
            cfg = prepare_config(
                provider,  # type: ignore[arg-type]
                suite=None,
                namespace=context.namespace,
                shared_namespace=context.shared_namespace,
                kube_context=kube_context,
                repo_root=context.paths.repo_root,
                distribution_root=context.paths.distribution_root,
            )
            selected = [candidate for candidate in cfg.inventory.products if candidate.id == product]
            if not selected:
                known = ", ".join(sorted(candidate.id for candidate in cfg.inventory.products))
                raise typer.Exit(code=fail(f"unknown product {product!r}; this project has: {known}."))
            prepare_kube_context(cfg)
            tags = {FULL_REFRESH_TAG: full_refresh} if full_refresh else None
            launch_and_poll_dagster_jobs(cfg, products=selected, tags=tags)
    except (E2EError, DeploymentError, ProviderContractError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
