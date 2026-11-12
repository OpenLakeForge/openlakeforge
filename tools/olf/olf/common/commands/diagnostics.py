"""Offline diagnostics commands."""

from __future__ import annotations

from pathlib import Path

import typer

from olf.common import config

app = typer.Typer(help="Inspect the checkout.")


@app.command("tree")
def tree() -> None:
    """Print the checkout tree without delegating to a shell utility."""
    root = config.repo_root()
    excluded = {".git", ".venv", ".terraform", "__pycache__", ".pytest_cache"}
    paths: list[Path] = []

    for directory, dirnames, filenames in root.walk():
        dirnames[:] = [name for name in dirnames if name not in excluded]
        paths.extend(
            directory / name
            for name in (*dirnames, *filenames)
            if name not in excluded
        )

    for path in sorted(paths):
        typer.echo(str(path.relative_to(root)))
