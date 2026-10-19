"""Connect GitHub sign-in (`issuer: github`) without a hand-made OAuth App."""

from __future__ import annotations

import os
import platform
import secrets
import webbrowser
from typing import Any

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
    from olf.deployment.engine import Toolkit
    from olf.deployment.kube_ops import namespace_exists

    context = deployment_context(
        "local",
        profile="",
        namespace="",
        cluster_name=cluster_name,
        kubeconfig_path=kubeconfig_path,
        project_root=project_root,
    )
    topology = context.topology
    access = topology.access
    github = topology.identity.github if topology.identity else None
    if github is None or access is None:
        raise typer.Exit(
            code=fail("openlakeforge.yaml has no spec.identity.github (issuer: github); nothing to connect")
        )
    host = github.host or "github.com"
    scheme = "http" if access.issuer == "none" else "https"
    callback = f"{scheme}://auth.{access.base_domain}/callback"
    ref, namespace = github.oauth_app_secret_ref, context.shared_namespace
    env = context.command_env(base=os.environ)
    kube = {"context": context.kube_context, "kubeconfig": context.paths.kubeconfig_path, "env": env}
    tools = Toolkit.default(environ=env)
    if not namespace_exists(tools.kubectl, namespace, **kube):
        raise typer.Exit(
            code=fail(f"namespace '{namespace}' does not exist; run olf deploy once (it creates it), then re-run this")
        )

    def manifest_for(redirect_url: str) -> dict[str, Any]:
        return github_app.build_manifest(
            name=f"openlakeforge-{topology.profile_name[:13]}-{secrets.token_hex(3)}",  # App names are unique on GitHub
            callback_url=callback,
            redirect_url=redirect_url,
            homepage=f"{scheme}://{access.base_domain}",
        )

    # WSL usually has no Linux browser, and xdg-open's failure is noise on the terminal; the URL is printed anyway.
    try_browser = not no_browser and "microsoft" not in platform.release().lower()

    def open_url(url: str) -> None:
        # Always print it: webbrowser.open reports success when xdg-open merely starts.
        log.info(f"Open this URL in your browser to create the GitHub App (5 minutes): {url}")
        if try_browser:
            webbrowser.open(url)

    try:
        code = github_app.wait_for_code(
            host=host, organization=github.organization, manifest_for=manifest_for, open_url=open_url
        )
        app_data = github_app.convert_code(code, host=host)
    except github_app.GitHubConnectError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    tools.kubectl.apply_from_manifest(
        github_app.secret_manifest(
            name=ref.name,
            namespace=namespace,
            values={
                ref.client_id_key: app_data["client_id"],
                ref.client_secret_key: app_data["client_secret"],
                "private-key": app_data["pem"],
            },
        ),
        **kube,
    )
    log.step(f"Stored the GitHub App credentials in Secret '{ref.name}' (namespace '{namespace}').")
    install = f"https://{host}/apps/{app_data['slug']}/installations/new"
    log.info(f"Install the app on {github.organization}: {install}")
    if try_browser:
        webbrowser.open(install)
    log.info("Next: olf deploy --provider local")
