"""Inspect the immutable platform payload embedded in the installed wheel."""

from __future__ import annotations

import json

import typer

from olf.distribution import DistributionError, DistributionManager, runtime_layout
from olf.log import fail

app = typer.Typer(help="Inspect and maintain the OpenLakeForge platform distribution.")


def _layout():  # noqa: ANN202
    return runtime_layout()


@app.command("list")
def list_distribution() -> None:
    """Show the distribution selected for this olf invocation."""
    try:
        layout = _layout()
    except DistributionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(f"mode {layout.mode}")
    typer.echo(f"version {layout.distribution_version}")
    typer.echo(f"root {layout.distribution_root}")
    typer.echo(f"project {layout.project_root}")
    typer.echo(f"payload-sha256 {layout.payload_sha256 or 'source-checkout'}")


@app.command("path")
def path() -> None:
    """Print the active immutable payload root."""
    try:
        typer.echo(str(_layout().distribution_root))
    except DistributionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


@app.command("verify")
def verify() -> None:
    """Hash-check the installed payload files."""
    try:
        layout = _layout()
        if layout.is_source:
            typer.echo("source checkout selected; no embedded payload to verify")
            return
        DistributionManager.from_embedded().verify()
    except DistributionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("verified")


@app.command("clean")
def clean() -> None:
    """Remove only this wheel version's extracted payload under OLF_HOME."""
    try:
        manager = DistributionManager.from_embedded()
        removed = manager.clean()
    except DistributionError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo("removed" if removed else "nothing to remove")


def version_payload() -> str:
    """Return JSON used by the root `olf version --json` command."""
    layout = _layout()
    return json.dumps(
        {
            "distribution_mode": layout.mode,
            "distribution_version": layout.distribution_version,
            "distribution_root": str(layout.distribution_root),
            "payload_sha256": layout.payload_sha256,
            "project_root": str(layout.project_root),
            "catalog_path": str(layout.catalog_path),
        },
        sort_keys=True,
    )
