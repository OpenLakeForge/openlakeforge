"""Connect GitHub sign-in (`issuer: github`) without a hand-made OAuth App."""

from __future__ import annotations

import typer

from olf import log
from olf.commands._shared import deployment_context, fail

app = typer.Typer(help="GitHub sign-in: create the GitHub App and store its credentials.")


@app.command("connect")
def connect(
    project_root: str = typer.Option(
        "", "--project", "--project-root", help="Project root; defaults to the current directory."
    ),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening a browser."),
) -> None:
    """Create the GitHub App for spec.identity.github in your browser and store it as the Secret
    `oauth_app_secret_ref` names. Re-running registers a new App and replaces the Secret; delete the old App yourself.
    """
    from olf import github_app

    context = deployment_context(
        "local",
        profile="",
        namespace="",
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    try:
        github_app.connect_github(context, no_browser=no_browser)
    except github_app.GitHubConnectError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    log.info("Next: olf deploy --provider local")
