"""`olf jobs`, `olf runs`, `olf logs`, `olf domain list` and `olf product list`: reads through the platform API.

Each sends the signed-in person's token and nothing else: the API decides what
the person may read (ADR 0021), so these run with no kubeconfig and no infra
credential, for a person or their local agent alike.
"""

from __future__ import annotations

import json
import unicodedata
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
# The same options for the commands that change state (olf/platform/writes.py).
PROFILE_OPTION, STAGE_OPTION, JSON_OPTION = _PROFILE, _STAGE, _JSON

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


def stage_path(stage: str, rest: str) -> str:
    return f"/v1/stages/{quote(stage, safe='')}/{rest}"


@jobs_app.command("list")
def jobs_list(stage: str = _STAGE, as_json: bool = _JSON, profile_file: str = _PROFILE) -> None:
    """List a stage's pipeline jobs."""
    jobs = _read(profile_file, stage_path(stage, "jobs"))["jobs"]
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
    runs = _read(profile_file, stage_path(stage, "runs"), params)["runs"]
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
    run = _read(profile_file, stage_path(stage, f"runs/{quote(run_id, safe='')}"))
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
    run_id: str = typer.Option("", "--run-id", help="Only the lines of this Dagster run, whatever its pod's name."),
    product: str = typer.Option("", "--product", help="Only this data product's lines."),
    component: str = typer.Option("", "--component", help="Only this component's lines, such as floe."),
    severity: str = typer.Option(
        "", "--severity", help="Only lines at this level or above: debug, info, warning, error or critical."
    ),
    grep: str = typer.Option(
        "", "--grep", help="Only lines whose message contains this text, ignoring case; plain text, not a pattern."
    ),
    since: str = typer.Option("", "--since", help="Only lines at or after this ISO 8601 time; UTC without an offset."),
    until: str = typer.Option("", "--until", help="Only lines before this ISO 8601 time; UTC without an offset."),
    limit: int = typer.Option(500, "--limit", min=1, max=5000, help="At most this many lines."),
    as_json: bool = _JSON,
    profile_file: str = _PROFILE,
) -> None:
    """Show a stage's archived pod logs for a day or one hour of it, oldest first.

    --run-id, --product and --component match records the node collector wrote; the older polling archive
    carries none of them. The newest lines may not be archived yet.
    """
    # Checked here because the API's own 422 for a bad date carries no message the command could show.
    try:
        when = date.fromisoformat(day) if day else datetime.now(UTC).date()
    except ValueError as exc:
        raise typer.Exit(code=fail(f"--date {day!r} is not a date in YYYY-MM-DD form")) from exc
    for flag, value in (("--since", since), ("--until", until)):
        try:
            if value:
                datetime.fromisoformat(value)
        except ValueError as exc:
            raise typer.Exit(code=fail(f"{flag} {value!r} is not an ISO 8601 time")) from exc
    if severity and severity not in ("debug", "info", "warning", "error", "critical"):
        raise typer.Exit(code=fail("--severity is one of debug, info, warning, error or critical"))
    # The API's limits on `q`, checked here so the refusal says which flag broke them.
    if len(grep) > 200 or any(unicodedata.category(char) == "Cc" for char in grep):
        raise typer.Exit(code=fail("--grep is at most 200 characters, with no control characters"))
    params: dict[str, Any] = {"date": when.isoformat(), "limit": limit}
    if hour is not None:
        params["hour"] = hour
    if pod:
        params["pod"] = pod
    if container:
        params["container"] = container
    for name, value in (
        ("run_id", run_id),
        ("product", product),
        ("component", component),
        ("severity", severity),
        ("q", grep),
        ("since", since),
        ("until", until),
    ):
        if value:
            params[name] = value
    result = _read(profile_file, stage_path(stage, "logs"), params)
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
