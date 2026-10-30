"""Bounded deployment smoke helpers."""

from __future__ import annotations

import typer

from olf.common.log import fail
from olf.infra import smoke as smoke_module

app = typer.Typer(help="Bounded deployment smoke helpers.")


@app.command("run")
def smoke_run(
    timeout_seconds: int = typer.Option(
        2700, "--timeout-seconds", help="Hard wall-clock limit for the complete smoke path."
    ),
) -> None:
    """Deploy the slim local stack and validate one product before the deadline."""
    try:
        smoke_module.run(timeout_seconds=timeout_seconds)
    except smoke_module.SmokeError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
