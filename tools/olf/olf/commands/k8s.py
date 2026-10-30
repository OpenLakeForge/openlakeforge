"""Kubernetes image bookkeeping helpers."""

from __future__ import annotations

import typer

from olf import config

app = typer.Typer(help="Kubernetes image bookkeeping helpers.")


@app.command("set-project-code-image")
def k8s_set_project_code_image(
    image: str = typer.Option(..., "--image", help="Fully qualified project-code image reference."),
    timeout: str = typer.Option("600s", "--timeout", help="Timeout for each Dagster deployment rollout."),
) -> None:
    """Point every Dagster surface at an image and wait for one rollout."""
    from olf import k8s

    k8s.set_project_code_image(image, config.namespace(), rollout_timeout=timeout)
