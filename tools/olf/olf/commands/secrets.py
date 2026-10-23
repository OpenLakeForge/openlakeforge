"""`olf secrets rotate`: rotate a workload secret in one command (#178)."""

from __future__ import annotations

import typer

from olf import log
from olf.commands._shared import deployment_context, deployment_context_for_profile, fail

app = typer.Typer(help="Rotate workload secrets.")


@app.command("rotate")
def rotate(
    target: str = typer.Argument(..., help="dex-client-perimeter, dex-client-trino or github-app."),
    profile_file: str = typer.Option(
        "", "--file", "-f", help="Deployment Profile v1 path, as for `olf platform apply -f`."
    ),
    project_root: str = typer.Option(
        "", "--project", "--project-root", help="Project root; defaults to the current directory."
    ),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Local kubeconfig file path override."),
    no_browser: bool = typer.Option(False, "--no-browser", help="github-app: print the URL instead of opening it."),
) -> None:
    """Delete TARGET's Secret, have the platform apply create it again, and restart what reads it.

    `github-app` registers a new GitHub App through `olf github connect`.
    """
    from olf import github_app, secrets_rotation
    from olf.deployment.engine import Toolkit
    from olf.deployment.errors import DeploymentError

    if target not in secrets_rotation.TARGETS:
        raise typer.Exit(code=fail(secrets_rotation.unknown_target_message(target)))
    if profile_file:
        # The profile-driven context, as `olf platform apply -f`: it leaves project-owned deployments alone.
        context = deployment_context_for_profile(
            profile_file, cluster_name=cluster_name, kubeconfig_path=kubeconfig_path
        )
    else:
        context = deployment_context(
            "local",
            profile="",
            namespace="",
            cluster_name=cluster_name,
            kubeconfig_path=kubeconfig_path,
            project_root=project_root,
        )
    try:
        kube = secrets_rotation.kube_args(context)
        kubectl = Toolkit.default(environ=kube["env"]).kubectl
        if target == secrets_rotation.GITHUB_APP:
            # Dex reads the Secret through secretKeyRef at start, so the new App is live only after a restart.
            install = github_app.connect_github(context, no_browser=no_browser)
            secrets_rotation.restart_and_wait(context, (secrets_rotation.DEX,), kubectl=kubectl)
            log.info("Rotated the GitHub App Secret and restarted deployment/dex.")
            log.info(f"Still to do: install the new app: {install}")
            log.info("Still to do: delete the old GitHub App in GitHub.")
            return
        rotation = secrets_rotation.rotate_client(context, target, kubectl=kubectl)
    except (secrets_rotation.RotationError, github_app.GitHubConnectError, DeploymentError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    log.info(f"Rotated Secret '{rotation.secret}' (namespace '{rotation.namespace}').")
    log.info("Restarted: " + ", ".join(str(r) for r in rotation.restarted) + ".")
    log.info("Nothing else to do.")
