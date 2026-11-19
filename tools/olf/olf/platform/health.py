"""`olf health` and `olf pipelines health`: the platform API's health rollups, shown and never re-evaluated.

The API owns every state (ADR 0021); this module only renders them. `unknown` and `disabled` print as themselves,
so neither can read as healthy. A service row carries when the API observed it and until when that answer is usable,
so an operator can tell a fresh answer from a stale one. Service rows are grouped under their service, headed by
the worst state among them, and carry the component's last durable write where the API reports one.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from openlakeforge_core.health import HealthState, worst_state

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


# An API older than #1287 sends none of `display_name`, `service`, `service_display_name` or `last_success_at`. Its
# rows still print, under this heading and with the component key as their name, rather than under an invented service.
_NO_SERVICE = "(service not reported)"
# A component with no durable-write evidence, or an API that does not report it. Never the probe time.
_NO_WRITE = "\u2014"


def _service_groups(services: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in services:
        heading = item.get("service_display_name") or item.get("service") or _NO_SERVICE
        groups.setdefault(heading, []).append(item)
    return groups


def _services(services: list[dict[str, Any]]) -> str:
    if not services:
        return "No service rows.\n"
    blocks = []
    for heading, items in _service_groups(services).items():
        state = worst_state(HealthState(item["state"]) for item in items)
        rows = [
            [
                item["component"],
                item.get("display_name") or item["component"],
                item["state"],
                item["observed_at"] or "never",
                item["expires_at"] or "-",
                item.get("last_success_at") or _NO_WRITE,
                item["reason"],
            ]
            for item in items
        ]
        columns = ["component", "name", "state", "observed", "valid until", "last write", "reason"]
        blocks.append(f"{heading}: {state.value}\n" + table(columns, rows))
    return "\n".join(blocks)


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
        typer.echo(_services(result["services"]))
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
