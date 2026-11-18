"""`olf alert`: offline views of the alert policy. No cluster, no credential (ADR 0020)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

import typer
import yaml
from openlakeforge_core import (
    LakehouseDescriptorError,
    load_alert_policy,
    load_governance_descriptor,
    resolve_conditions,
)
from openlakeforge_core.alert_policy import CONDITIONS, JOB_SUFFIX, STAGES, AlertPolicy, ConditionOptions, Freshness

from olf.common.commands._project import writable_project_root

app = typer.Typer(help="Offline views of the alert policy (`lakehouse_code/alerts.yml`).")


def _duration(value: timedelta) -> str:
    minutes = int(value.total_seconds() // 60)
    for unit, size in (("d", 1440), ("h", 60)):
        if minutes % size == 0:
            return f"{minutes // size}{unit}"
    return f"{minutes}m"


def _options(options: ConditionOptions) -> str:
    fields = asdict(options)
    if isinstance(options, Freshness) and (options.warn_after or options.critical_after):
        # Thresholds replace the cadence options, so their defaults would read as settings.
        fields.pop("grace"), fields.pop("severity")
    parts = [
        f"{key}={_duration(value) if isinstance(value, timedelta) else value}"
        for key, value in fields.items()
        if value is not None
    ]
    return ", ".join(parts) or "defaults"


def _scope(policy: AlertPolicy, index: int) -> str:
    rule = policy.rules[index]
    return f"rules[{index}] ({rule.domain}/{rule.product})"


@app.command("explain")
def explain(
    stage: str = typer.Option(..., "--stage", help="dev, uat or prod."),
    product: str = typer.Option("", "--product", help="A product id of governance.yml."),
    job: str = typer.Option("", "--job", help="A Dagster job name, such as a maintenance job of no product."),
    project: str = typer.Option("", "--project", help="Project root; the current project by default."),
) -> None:
    """Print, per condition, the rule that wins for a product or a job in a stage, its options and its routes.

    A job resolves as the evaluator resolves it: `<product>_pipeline` as that product, any other job by the global
    rules alone. Exit 0 when the policy was read, including a project with no alerts.yml. Exit 1 when the project,
    the policy or the product is invalid; exit 2 for an unknown stage or unless exactly one of --product and --job
    is given.
    """
    if stage not in STAGES:
        typer.echo(f"unknown stage {stage!r}; stages are {', '.join(STAGES)}")
        raise typer.Exit(code=2)
    if bool(product) == bool(job):
        typer.echo("give exactly one of --product and --job")
        raise typer.Exit(code=2)
    try:
        code = writable_project_root(project) / "lakehouse_code"
        governance = load_governance_descriptor(code / "governance.yml")
        domains = {p["id"]: d["name"] for d in governance["domains"] for p in d.get("products", [])}
        if product and product not in domains:
            raise LakehouseDescriptorError(f"{product!r} is not a product of governance.yml ({sorted(domains)})")
        if job:
            owner = job.removesuffix(JOB_SUFFIX) if job.endswith(JOB_SUFFIX) else ""
            product = owner if owner in domains else ""
        subject = product or job
        policy_path = code / "alerts.yml"
        if not policy_path.is_file():
            typer.echo(f"No alerts.yml: nothing alerts for {subject} in {stage}.")
            return
        policy = load_alert_policy(policy_path, governance)
    except (LakehouseDescriptorError, RuntimeError, OSError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    except yaml.YAMLError as exc:
        typer.echo(f"alerts.yml or governance.yml is not valid YAML: {exc}")
        raise typer.Exit(code=1) from exc

    if product:
        resolved = resolve_conditions(policy, stage, domain=domains[product], product=product)
        typer.echo(f"{product} (domain {domains[product]}, job {product}{JOB_SUFFIX}) in {stage}")
    else:
        resolved = resolve_conditions(policy, stage)
        typer.echo(f"job {job} (no product: global rules only) in {stage}")
    for condition in CONDITIONS:
        winner = resolved.get(condition)
        if winner is None:
            typer.echo(f"  {condition}: not declared, does not alert")
        elif winner.options is None:
            typer.echo(f"  {condition}: off by {_scope(policy, winner.rule)}")
        else:
            routes = ", ".join(f"{route} -> {policy.routes[route]}" for route in winner.routes)
            typer.echo(f"  {condition}: {_options(winner.options)}; {_scope(policy, winner.rule)}; notifies {routes}")
