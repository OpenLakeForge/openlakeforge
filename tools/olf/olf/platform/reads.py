"""`olf jobs`, `olf runs`, `olf logs`, `olf domain list` and `olf product list`: reads through the platform API.

Each sends the signed-in person's token and nothing else: the API decides what
the person may read (ADR 0021), so these run with no kubeconfig and no infra
credential, for a person or their local agent alike.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
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
runs_app = typer.Typer(help="Pipeline runs of a stage, read through the platform API.", no_args_is_help=True)


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


def logs(
    stage: str = _STAGE,
    day: str = typer.Option("", "--date", help="Day to read, YYYY-MM-DD; today in UTC by default."),
    hour: int | None = typer.Option(None, "--hour", min=0, max=23, help="Only this hour of the day, UTC."),
    pod: str = typer.Option("", "--pod", help="Only this pod's lines."),
    container: str = typer.Option("", "--container", help="Only this container's lines."),
    limit: int = typer.Option(500, "--limit", min=1, max=5000, help="At most this many lines."),
    as_json: bool = _JSON,
    profile_file: str = _PROFILE,
) -> None:
    """Show a stage's archived pod logs for a day or one hour of it, oldest archive first.

    The archive is written hourly, so the current hour may not be in it yet.
    """
    # Checked here because the API's own 422 for a bad date carries no message the command could show.
    try:
        when = date.fromisoformat(day) if day else datetime.now(UTC).date()
    except ValueError as exc:
        raise typer.Exit(code=fail(f"--date {day!r} is not a date in YYYY-MM-DD form")) from exc
    params: dict[str, Any] = {"date": when.isoformat(), "limit": limit}
    if hour is not None:
        params["hour"] = hour
    if pod:
        params["pod"] = pod
    if container:
        params["container"] = container
    result = _read(profile_file, _stage_path(stage, "logs"), params)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    for line in result["lines"]:
        source = f"{line['pod']}/{line['container']}"
        typer.echo(
            f"{source}  {(line['line'] or '') if line['error'] is None else '[not archived: ' + line['error'] + ']'}"
        )
    if result["truncated"]:
        typer.echo(
            f"Showing the first {limit} lines; narrow with --hour, --pod or --container, or raise --limit.", err=True
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
