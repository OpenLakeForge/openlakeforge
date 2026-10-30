"""Local image build and Kind-load commands."""

from __future__ import annotations

import os

import typer

from olf.common.deployment.errors import DeploymentError
from olf.common.image_build import build_project_code_image, build_superset_image, local_image_config
from olf.common.log import fail
from olf.infra.deployment.local.images import load_image_into_kind

app = typer.Typer(help="Build and load local runtime images.")


@app.command("build")
def build(
    image: str = typer.Argument(..., metavar="IMAGE", help="project-code or superset."),
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
        else:
            raise ValueError("expected 'project-code' or 'superset'")
    except (DeploymentError, ValueError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


@app.command("load")
def load(
    image: str = typer.Argument(..., metavar="IMAGE", help="project-code or superset."),
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
        else:
            raise ValueError("expected 'project-code' or 'superset'")
        load_image_into_kind(reference, settings, tools, env=os.environ)
    except (DeploymentError, ValueError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
