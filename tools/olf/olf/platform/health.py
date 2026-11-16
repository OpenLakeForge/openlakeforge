"""`olf health` and `olf pipelines health`: the platform API's health rollups, shown and never re-evaluated.

The API owns every state (ADR 0021); this module only renders them. `unknown` and `disabled` print as themselves,
so neither can read as healthy.
"""

from __future__ import annotations

import json
from typing import Any

import typer

from olf.platform.output import table
from olf.platform.reads import JSON_OPTION, PROFILE_OPTION, _read, stage_path

# `warning` and `disabled` exit 0: the stage still works, or has nothing to evaluate. `unknown` fails because a
# rollup that cannot see its sources must not pass a script's gate.
FAILING_STATES = frozenset({"critical", "unknown"})

pipelines_app = typer.Typer(help="Pipelines of a stage, read through the platform API.", no_args_is_help=True)

_STAGE = typer.Argument(..., help="Stage to read, such as dev.")


def _reason(pipeline: dict[str, Any]) -> str:
    # The signals that put the pipeline in its state are the explanation; a healthy pipeline has none to give.
    reasons = [signal["reason"] for signal in pipeline["signals"] if signal["state"] == pipeline["state"]]
    return "; ".join(reasons) or "-"


def _integrations(result: dict[str, Any]) -> None:
    for integration in result["integrations"]:
        if integration["status"] != "available":
            typer.echo(f"{integration['name']} is {integration['status']}: {integration['reason']}")


def health(stage: str = _STAGE, as_json: bool = JSON_OPTION, profile_file: str = PROFILE_OPTION) -> None:
    """Show a stage's health: its state, services and pipelines.

    Exits 0 for healthy, warning and disabled; 1 for critical and unknown, and when the API cannot be read.
    """
    result = _read(profile_file, stage_path(stage, "health"))
    if as_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(f"Stage {result['stage']}: {result['state']} (evaluated {result['evaluated_at']})\n")
        typer.echo(
            table(
                ["service", "state", "reason"],
                [[item["component"], item["state"], item["reason"]] for item in result["services"]],
            )
        )
        typer.echo(table(["pipeline", "state"], [[item["job"], item["state"]] for item in result["pipelines"]]))
        _integrations(result)
    if result["state"] in FAILING_STATES:
        raise typer.Exit(code=1)


@pipelines_app.command("health")
def pipelines_health(stage: str = _STAGE, as_json: bool = JSON_OPTION, profile_file: str = PROFILE_OPTION) -> None:
    """Show each pipeline's health and why.

    Exits 0 when every pipeline is healthy, warning or disabled; 1 when any is critical or unknown, and when the
    API cannot be read.
    """
    result = _read(profile_file, stage_path(stage, "health/pipelines"))
    if as_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(
            table(
                ["pipeline", "state", "reason"],
                [[item["job"], item["state"], _reason(item)] for item in result["pipelines"]],
            )
        )
        _integrations(result)
    if any(item["state"] in FAILING_STATES for item in result["pipelines"]):
        raise typer.Exit(code=1)
