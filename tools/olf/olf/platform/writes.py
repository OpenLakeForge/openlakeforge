"""`olf runs launch`: a state-changing call to the platform API, as the signed-in person.

The API decides whether the person may launch on the stage and records the
attempt before it acts (ADR 0021, decision 3), so this command sends the request
once and never retries it.
"""

from __future__ import annotations

import json

import typer

from olf.common.log import fail
from olf.platform import api
from olf.platform import session as sessions
from olf.platform.reads import JSON_OPTION, PROFILE_OPTION, STAGE_OPTION, stage_path


def runs_launch(
    job: str = typer.Argument(..., help="The job to launch, as `olf jobs list` prints it."),
    stage: str = STAGE_OPTION,
    as_json: bool = JSON_OPTION,
    profile_file: str = PROFILE_OPTION,
) -> None:
    """Launch one of a stage's jobs as you; the API records who asked for it."""
    try:
        run = api.post(sessions.resolve_instance(profile_file), stage_path(stage, "runs"), {"job": job})
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if as_json:
        typer.echo(json.dumps(run, indent=2))
        return
    typer.echo(f"Launched {run['job']} on {run['stage']}: run {run['run_id']}")
    typer.echo(f"Follow it with `olf runs show {run['run_id']} --stage {run['stage']}`.")
