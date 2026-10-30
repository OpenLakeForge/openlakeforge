"""`olf infra query`: run SQL against a stage's Trino catalog."""

from __future__ import annotations

from pathlib import Path

import typer

from olf.common.log import fail
from olf.infra import trino
from olf.infra.commands._shared import applied_runtime_environment, deployment_context_for_profile

# Trino CLI --output-format values for each --format choice.
OUTPUT_FORMATS = {"table": "ALIGNED", "csv": "CSV_HEADER", "json": "JSON"}


def query(
    sql: str = typer.Argument("", help="SQL to run. Use --file to read it from a file instead."),
    file: str = typer.Option("", "--file", help="Read the SQL from this file."),
    stage: str = typer.Option("", "--stage", help="Stage to query: dev, uat, or prod. Defaults to dev."),
    output_format: str = typer.Option("table", "--format", help="Output format: table, csv, or json."),
    profile_file: str = typer.Option(
        "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile the deployment was applied from."
    ),
) -> None:
    """Run SQL against a stage's Trino catalog, for inspection.

    The stage's catalog is the session default, so SQL names `<schema>.<table>`,
    for example `order_revenue_gold.mart_order_revenue_by_day`.

    The query runs as the stage's shared runtime workload user, which can write
    as well as read. Use it to look at data, not to load it. It is not
    per-user access: every caller of this command is the same Trino user.
    """
    from olf.common.deployment.errors import DeploymentError
    from olf.common.provider_contracts import ProviderContractError
    from olf.common.tooling.resolver import build_resolver

    if output_format not in OUTPUT_FORMATS:
        raise typer.Exit(code=fail(f"unknown --format {output_format!r}; expected table, csv, or json."))
    if bool(sql) == bool(file):
        raise typer.Exit(code=fail("give the SQL either as an argument or with --file."))
    if file:
        try:
            sql = Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.Exit(code=fail(f"cannot read {file}: {exc}")) from exc
    if not sql.strip():
        raise typer.Exit(code=fail("the SQL is empty."))

    context = deployment_context_for_profile(profile_file, stage=stage)
    provider = context.provider.value
    try:
        with applied_runtime_environment(context, env=provider) as kube_context:
            catalog = trino.stage_catalog_name()
            if trino.glue_namespace_prefix(provider, catalog):
                # ponytail: AWS stages share one Glue catalog behind a name prefix that
                # SQL would have to be rewritten for; add it once the AWS root is stage-aware.
                msg = "olf infra query does not support the aws provider yet: Glue schema names are prefixed per stage."
                raise typer.Exit(code=fail(msg))
            target = trino.TrinoTarget(
                provider=provider,
                kube_context=kube_context,
                namespace=context.shared_namespace or context.namespace,
                catalog=catalog,
                stage=context.stage.value,
            )
            kubectl = str(build_resolver().resolve("kubectl"))
            output = trino.run_sql(target, sql, output_format=OUTPUT_FORMATS[output_format], kubectl=kubectl)
    except (trino.TrinoQueryError, DeploymentError, ProviderContractError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(output, nl=False)
