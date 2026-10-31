"""`olf jobs`, `olf runs`, `olf domain list` and `olf product list`: platform reads as clients of the platform API.

Each sends the signed-in person's token and nothing else: the API decides what
the person may read or launch (ADR 0021), so these run with no kubeconfig and no infra
credential, for a person or their local agent alike.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import typer

from olf.common.log import fail
from olf.platform import api
from olf.platform import session as sessions
from olf.platform.output import table

_PROFILE = typer.Option(
    "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile of the deployment to read."
)
# The API answers 404 for a stage the deployment does not enable, so no stage list is kept here.
_STAGE = typer.Option("dev", "--stage", help="Stage to read.")
_JSON = typer.Option(False, "--json", help="Print the platform API's JSON instead of a table.")

jobs_app = typer.Typer(help="Pipeline jobs of a stage, read through the platform API.", no_args_is_help=True)
runs_app = typer.Typer(help="Pipeline runs of a stage, through the platform API.", no_args_is_help=True)


def _read(profile_file: str, path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        return api.get(sessions.resolve_instance(profile_file), path, params)
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def _show(as_json: bool, data: Any, columns: Sequence[str], rows: Callable[[Any], list[list[str]]]) -> None:
    typer.echo(json.dumps(data, indent=2) if as_json else table(columns, rows(data)), nl=as_json)


def _time(seconds: float | None) -> str:
    return "-" if seconds is None else datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def _duration(run: dict[str, Any]) -> str:
    if run.get("started_at") is None or run.get("ended_at") is None:
        return "-"
    return f"{run['ended_at'] - run['started_at']:.0f}s"


def _stage_path(stage: str, rest: str) -> str:
    return f"/v1/stages/{quote(stage, safe='')}/{rest}"


@jobs_app.command("list")
def jobs_list(stage: str = _STAGE, as_json: bool = _JSON, profile_file: str = _PROFILE) -> None:
    """List a stage's pipeline jobs."""
    jobs = _read(profile_file, _stage_path(stage, "jobs"))["jobs"]
    _show(
        as_json,
        jobs,
        ["job", "location", "description"],
        lambda items: [[job["name"], job["location"], job["description"]] for job in items],
    )


@runs_app.command("list")
def runs_list(
    stage: str = _STAGE,
    job: str = typer.Option("", "--job", help="Only this job's runs."),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="How many of the most recent runs."),
    as_json: bool = _JSON,
    profile_file: str = _PROFILE,
) -> None:
    """List a stage's most recent pipeline runs, newest first."""
    params: dict[str, Any] = {"limit": limit, **({"job": job} if job else {})}
    runs = _read(profile_file, _stage_path(stage, "runs"), params)["runs"]
    _show(
        as_json,
        runs,
        ["run", "job", "status", "created", "duration"],
        lambda items: [
            [run["run_id"], run["job_name"], run["status"], _time(run["created_at"]), _duration(run)] for run in items
        ],
    )


@runs_app.command("launch")
def runs_launch(
    job: str = typer.Argument(..., help="The job to launch, as `olf jobs list` prints it."),
    stage: str = _STAGE,
    as_json: bool = _JSON,
    profile_file: str = _PROFILE,
) -> None:
    """Launch one of a stage's jobs as you; the API records who asked for it."""
    try:
        run = api.post(sessions.resolve_instance(profile_file), _stage_path(stage, "runs"), {"job": job})
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if as_json:
        typer.echo(json.dumps(run, indent=2))
        return
    typer.echo(f"Launched {run['job']} on {run['stage']}: run {run['run_id']}")
    typer.echo(f"Follow it with `olf runs show {run['run_id']} --stage {run['stage']}`.")


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="The run's id, as `olf runs list` prints it."),
    stage: str = _STAGE,
    as_json: bool = _JSON,
    profile_file: str = _PROFILE,
) -> None:
    """Show one run's status, times and tags."""
    run = _read(profile_file, _stage_path(stage, f"runs/{quote(run_id, safe='')}"))
    _show(
        as_json,
        run,
        ["field", "value"],
        lambda item: [
            ["run", item["run_id"]],
            ["job", item["job_name"]],
            ["status", item["status"]],
            ["created", _time(item["created_at"])],
            ["started", _time(item["started_at"])],
            ["ended", _time(item["ended_at"])],
            ["duration", _duration(item)],
            *([f"tag {key}", str(value)] for key, value in sorted(item["tags"].items())),
        ],
    )


def domain_list(as_json: bool = _JSON, profile_file: str = _PROFILE) -> None:
    """List the governance domains, as OpenMetadata shows them to you."""
    domains = _read(profile_file, "/v1/domains")["domains"]
    _show(
        as_json,
        domains,
        ["domain", "display name"],
        lambda items: [[domain["name"], domain["display_name"]] for domain in items],
    )


def product_list(as_json: bool = _JSON, profile_file: str = _PROFILE) -> None:
    """List the data products and their domains, as OpenMetadata shows them to you."""
    products = _read(profile_file, "/v1/data-products")["data_products"]
    _show(
        as_json,
        products,
        ["product", "display name", "domains"],
        lambda items: [[item["name"], item["display_name"], ", ".join(item["domains"])] for item in items],
    )
