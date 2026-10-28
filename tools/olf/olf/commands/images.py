"""Local image build and Kind-load commands."""

from __future__ import annotations

import os
from collections.abc import Mapping

import typer

from olf import config
from olf.commands._shared import fail
from olf.deployment.context import DeploymentContext, Profile
from olf.deployment.engine import Toolkit
from olf.deployment.errors import DeploymentError
from olf.deployment.local.config import LocalDeploymentConfig
from olf.deployment.local.images import build_project_code_image, build_superset_image, load_image_into_kind

app = typer.Typer(help="Build and load local runtime images.")


def _local_config(
    cluster_name: str, *, project_root: str = "", environ: Mapping[str, str] | None = None
) -> tuple[LocalDeploymentConfig, Toolkit]:
    from olf.distribution import runtime_layout

    layout_env = dict(os.environ if environ is None else environ)
    if project_root:
        layout_env["OPENLAKEFORGE_PROJECT_ROOT"] = project_root
    layout = runtime_layout(layout_env)
    context = DeploymentContext.for_provider(
        "local",
        repo_root=layout.project.root,
        distribution_root=layout.project.distribution_root,
        state_root=None if layout.is_source else layout.state_root,
        work_root=None if layout.is_source else layout.work_root,
        cache_root=None if layout.is_source else layout.cache_root,
        namespace=config.namespace(),
        cluster_name=cluster_name,
        profile=Profile.FULL,
    )
    resolved_environ = os.environ if environ is None else environ
    return LocalDeploymentConfig.from_environment(resolved_environ, context=context), Toolkit.default()


@app.command("build")
def build(
    image: str = typer.Argument(..., metavar="IMAGE", help="project-code or superset."),
    cluster_name: str = typer.Option("openlakeforge-local", "--cluster-name"),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Build one locally pinned runtime image with Docker retry policy."""
    settings, tools = _local_config(cluster_name, project_root=project_root)
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
    settings, tools = _local_config(cluster_name, project_root=project_root)
    try:
        if image == "project-code":
            reference = settings.images.project_code_image
        elif image == "superset":
            reference = settings.images.superset_image
        else:
            raise ValueError("expected 'project-code' or 'superset'")
        load_image_into_kind(reference, settings, tools, env=os.environ)
    except (DeploymentError, ValueError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
