"""`olf product new` -- golden-path product (Gold) scaffolding; `olf infra product run` -- launch its pipeline."""

from __future__ import annotations

import typer

from olf.common.log import fail
from olf.infra.commands._shared import applied_runtime_environment, deployment_context_for_profile

app = typer.Typer(help="Run product pipelines.")


@app.command("run")
def product_run(
    product: str = typer.Argument(..., help="The product whose pipeline to run, e.g. 'order_revenue'."),
    stage: str = typer.Option("", "--stage", help="Stage to run in: dev, uat, or prod. Defaults to dev."),
    full_refresh: str = typer.Option(
        "",
        "--full-refresh",
        help="Full refresh for this run: 'bronze' re-extracts incremental Bronze resources and rebuilds Gold, "
        "'gold' rebuilds Gold only.",
    ),
    profile_file: str = typer.Option(
        "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile the deployment was applied from."
    ),
) -> None:
    """Launch a product's pipeline in a stage and wait for it to finish.

    The run loads Bronze as each resource's `source.yaml` load mode declares,
    and dbt builds Gold. A full refresh applies to this one run only.
    """
    from openlakeforge_domain import FULL_REFRESH_SCOPES, full_refresh_tags

    from olf.common.deployment.errors import DeploymentError
    from olf.common.provider_contracts import ProviderContractError
    from olf.infra.e2e._dagster import launch_and_poll_dagster_jobs
    from olf.infra.e2e._runner import prepare_config, prepare_kube_context
    from olf.infra.e2e._shell import E2EError

    if full_refresh and full_refresh not in FULL_REFRESH_SCOPES:
        expected = " or ".join(FULL_REFRESH_SCOPES)
        raise typer.Exit(code=fail(f"unknown --full-refresh {full_refresh!r}; expected {expected}."))
    context = deployment_context_for_profile(profile_file, stage=stage)
    provider = context.provider.value
    try:
        with applied_runtime_environment(context, env=provider) as kube_context:
            cfg = prepare_config(
                provider,
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
            tags = full_refresh_tags(full_refresh) if full_refresh else None
            launch_and_poll_dagster_jobs(cfg, products=selected, tags=tags)
    except (E2EError, DeploymentError, ProviderContractError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
