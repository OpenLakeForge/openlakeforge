"""Register the GitHub App that Dex signs people in with, through GitHub's manifest flow.

GitHub has no API to create an OAuth App, but it creates a GitHub App from a manifest the operator
confirms in the browser. A GitHub App's client id and secret drive Dex's github connector the same way
(ADR 0014). No credential is printed or logged here: the conversion response goes straight into the Secret.
"""

from __future__ import annotations

import base64
import html
import json
import os
import platform
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

TIMEOUT_SECONDS = 300


class GitHubConnectError(RuntimeError):
    pass


def build_manifest(*, name: str, callback_url: str, redirect_url: str, homepage: str) -> dict[str, Any]:
    return {
        "name": name[:34],  # GitHub's App name limit
        "url": homepage,
        "callback_urls": [callback_url],
        "redirect_url": redirect_url,
        "public": False,
        # members: team listing and membership for the sign-in filter; emails: Dex reads the primary address.
        "default_permissions": {"members": "read", "emails": "read"},
    }


def _form_page(post_url: str, manifest: Mapping[str, Any]) -> bytes:
    return (
        f'<!doctype html><title>OpenLakeForge</title><form id="f" method="post" action="{html.escape(post_url)}">'
        f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">'
        "<noscript><button>Continue to GitHub</button></noscript></form>"
        '<p>Redirecting to GitHub...</p><script>document.getElementById("f").submit()</script>'
    ).encode()


def wait_for_code(
    *,
    host: str,
    organization: str,
    manifest_for: Callable[[str], Mapping[str, Any]],
    open_url: Callable[[str], None],
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Serve the manifest on 127.0.0.1, hand the local URL to `open_url`, and return the one-time code
    GitHub redirects back with. `manifest_for(redirect_url)` builds the manifest once the port is known."""
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    server: HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/":
                post = f"https://{host}/organizations/{organization}/settings/apps/new?state={state}"
                redirect = f"http://127.0.0.1:{server.server_port}/done"
                body, status = _form_page(post, manifest_for(redirect)), 200
            elif url.path == "/done" and query.get("state") == [state] and query.get("code"):
                result["code"] = query["code"][0]
                body, status = b"<p>GitHub App created. You can close this tab and return to the terminal.</p>", 200
            else:
                body, status = b"not found", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # request lines carry the one-time code

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    open_url(f"http://127.0.0.1:{server.server_port}/")
    deadline = time.monotonic() + timeout
    try:
        while "code" not in result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise GitHubConnectError(f"no GitHub App was created within {int(timeout)}s; nothing was written")
    return result["code"]


def api_base(host: str) -> str:
    return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"


def convert_code(code: str, *, host: str) -> dict[str, Any]:
    """Exchange the manifest code for the App's credentials (unauthenticated, single use)."""
    request = urllib.request.Request(
        f"{api_base(host)}/app-manifests/{urllib.parse.quote(code, safe='')}/conversions",
        data=b"",
        method="POST",
        headers={"Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https URL built above
            data: dict[str, Any] = json.load(response)
    except (OSError, ValueError) as exc:
        raise GitHubConnectError(f"GitHub rejected the manifest code: {type(exc).__name__}") from exc
    if not {"client_id", "client_secret", "pem", "slug"} <= data.keys():
        raise GitHubConnectError("GitHub's conversion response lacks the App credentials")
    return data


def secret_manifest(*, name: str, namespace: str, values: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()},
        }
    )


def connect_github(context: Any, *, no_browser: bool = False) -> str:
    """Create the GitHub App for spec.identity.github, store it as the Secret `oauth_app_secret_ref` names, and
    return the install link. Shared by `olf github connect` and the `olf deploy` preflight."""
    from olf import log
    from olf.deployment.engine import Toolkit
    from olf.deployment.kube_ops import namespace_exists

    topology = context.topology
    access = topology.access
    github = topology.identity.github if topology.identity else None
    if github is None or access is None:
        raise GitHubConnectError("openlakeforge.yaml has no spec.identity.github (issuer: github); nothing to connect")
    host = github.host or "github.com"
    scheme = "http" if access.issuer == "none" else "https"
    callback = f"{scheme}://auth.{access.base_domain}/callback"
    ref, namespace = github.oauth_app_secret_ref, context.shared_namespace
    env = context.command_env(base=os.environ)
    kube = {"context": context.kube_context, "kubeconfig": context.paths.kubeconfig_path, "env": env}
    tools = Toolkit.default(environ=env)
    if not namespace_exists(tools.kubectl, namespace, **kube):
        raise GitHubConnectError(
            f"namespace '{namespace}' does not exist; run olf deploy once (it creates it), then re-run this"
        )

    def manifest_for(redirect_url: str) -> dict[str, Any]:
        return build_manifest(
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

    code = wait_for_code(host=host, organization=github.organization, manifest_for=manifest_for, open_url=open_url)
    app_data = convert_code(code, host=host)
    tools.kubectl.apply_from_manifest(
        secret_manifest(
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
    return install
