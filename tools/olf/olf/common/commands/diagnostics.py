"""Offline diagnostics commands."""

from __future__ import annotations

import typer

from olf.common import config

app = typer.Typer(help="Inspect the checkout.")


@app.command("tree")
def tree() -> None:
    """Print the checkout tree without delegating to a shell utility."""
    root = config.repo_root()
    excluded = {".git", ".venv", ".terraform", "__pycache__", ".pytest_cache"}
    for directory, dirnames, filenames in root.walk():
        dirnames[:] = sorted(name for name in dirnames if name not in excluded)
        for name in sorted([*dirnames, *filenames]):
            path = directory / name
            typer.echo(str(path.relative_to(root)))
