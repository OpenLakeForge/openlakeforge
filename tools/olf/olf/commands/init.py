"""`olf init` -- initialize a writable OpenLakeForge project."""

from __future__ import annotations

import typer

from olf.commands._shared import fail
from olf.initialization import InitializationError, initialize_project
from olf.profile import Preset, StageName


def initialize(
    empty: bool = typer.Option(False, "--empty", help="Create a transitional empty project instead of the demo."),
    stages: str = typer.Option(
        "dev", "--stages", help="Comma-separated stages the profile enables: dev, or e.g. dev,prod or dev,uat,prod."
    ),
    preset: str = typer.Option("slim", "--preset", help="slim, or full to enable analytics and governance."),
    name: str = typer.Option("", "--name", help="Profile name; defaults to the project directory's name."),
    profile_only: bool = typer.Option(
        False, "--profile-only", help="Write only openlakeforge.yaml, e.g. into an existing 0.2 project."
    ),
) -> None:
    """Create a writable lakehouse project, and its Deployment Profile, in the current directory."""
    try:
        selected = [StageName(stage.strip()) for stage in stages.split(",") if stage.strip()]
        selected_preset = Preset(preset)
    except ValueError as exc:
        raise typer.Exit(code=fail(f"--stages takes dev, uat, prod and --preset slim or full: {exc}")) from exc
    try:
        result = initialize_project(
            empty=empty, stages=selected, preset=selected_preset, name=name, profile_only=profile_only
        )
    except InitializationError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if result.profile_only:
        typer.echo(f"Wrote {result.project_root / 'openlakeforge.yaml'}")
    else:
        typer.echo(f"Initialized {result.lakehouse_root}")
    typer.echo(f"Next: {result.next_command}")
