"""Local image build command."""

from __future__ import annotations

import os

import typer

from olf.common.deployment.errors import DeploymentError
from olf.common.image_build import (
    build_platform_api_image,
    build_project_code_image,
    build_superset_image,
    local_image_config,
)
from olf.common.log import fail

app = typer.Typer(help="Build local runtime images.")


@app.command("build")
def build(
    image: str = typer.Argument(..., metavar="IMAGE", help="project-code, superset or platform-api."),
    cluster_name: str = typer.Option("openlakeforge-local", "--cluster-name"),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Build one locally pinned runtime image with Docker retry policy."""
    settings, tools = local_image_config(cluster_name, project_root=project_root)
    try:
        if image == "project-code":
            build_project_code_image(settings, tools, env=os.environ)
        elif image == "superset":
            build_superset_image(settings, tools, env=os.environ)
        elif image == "platform-api":
            build_platform_api_image(settings, tools, env=os.environ)
        else:
            raise ValueError("expected 'project-code', 'superset' or 'platform-api'")
    except (DeploymentError, ValueError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
