"""Offline diagnostics commands."""

from __future__ import annotations

import typer

from olf.common import config

app = typer.Typer(help="Inspect the checkout.")


@app.command("tree")
def tree() -> None:
    """Print the checkout tree without delegating to a shell utility."""
    root = config.repo_root()
    for path in sorted(root.rglob("*")):
        if ".git" not in path.parts:
            typer.echo(str(path.relative_to(root)))
