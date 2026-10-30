"""Optional platform-layer capability helpers."""

from __future__ import annotations

import os

import typer

from olf.common import layers as layers_module

app = typer.Typer(help="Optional platform-layer capability helpers.")


@app.command("enabled")
def layers_enabled(
    layer: str = typer.Option(..., "--layer", help="Optional layer: analytics or governance."),
) -> None:
    """Exit successfully only when an optional platform layer is enabled."""
    from olf.common.log import fail

    try:
        is_enabled = layers_module.enabled(os.environ, layer)
    except ValueError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if not is_enabled:
        raise typer.Exit(code=1)
