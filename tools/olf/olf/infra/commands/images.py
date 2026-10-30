"""Local image build and Kind-load commands."""

from __future__ import annotations

import os

import typer

from olf.common.deployment.errors import DeploymentError
from olf.common.image_build import local_image_config
from olf.common.log import fail
from olf.infra.deployment.local.images import load_image_into_kind

app = typer.Typer(help="Load local runtime images into kind.")


@app.command("load")
def load(
    image: str = typer.Argument(..., metavar="IMAGE", help="project-code, superset or platform-api."),
    cluster_name: str = typer.Option("openlakeforge-local", "--cluster-name"),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Load a built runtime image into the required Kind cluster."""
    settings, tools = local_image_config(cluster_name, project_root=project_root)
    try:
        if image == "project-code":
            reference = settings.images.project_code_build_image
        elif image == "superset":
            reference = settings.images.superset_image
        elif image == "platform-api":
            reference = settings.images.platform_api_build_image
        else:
            raise ValueError("expected 'project-code', 'superset' or 'platform-api'")
        load_image_into_kind(reference, settings, tools, env=os.environ)
    except (DeploymentError, ValueError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
