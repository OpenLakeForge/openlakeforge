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
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

TIMEOUT_SECONDS = 900


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
    on_code: Callable[[str], str],
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Serve the manifest on 127.0.0.1, hand the local URL to `open_url`, and pass the one-time code GitHub
    redirects back with to `on_code`. `on_code` returns the URL the browser is sent on to (the app install page);
    a GitHubConnectError from it shows an error page and is raised here. `manifest_for(redirect_url)` builds the
    manifest once the port is known."""
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    failure: list[Exception] = []
    server: HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            location = None
            if url.path == "/":
                post = f"https://{host}/organizations/{organization}/settings/apps/new?state={state}"
                redirect = f"http://127.0.0.1:{server.server_port}/done"
                body, status = _form_page(post, manifest_for(redirect)), 200
            elif url.path == "/done" and query.get("state") == [state] and query.get("code"):
                try:
                    result["next"] = location = on_code(query["code"][0])
                    body, status = b"<p>Redirecting to the app install page...</p>", 302
                except Exception as exc:  # re-raised below; the loop would otherwise wait out the timeout
                    failure.append(exc)
                    body, status = f"<p>{html.escape(str(exc))}</p><p>See the terminal.</p>".encode(), 500
            else:
                body, status = b"not found", 404
            self.send_response(status)
            if location:
                self.send_header("Location", location)
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
        while "next" not in result and not failure and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if failure:
        raise failure[0]
    if "next" not in result:
        raise GitHubConnectError(f"no GitHub App was created within {int(timeout)}s; nothing was written")
    return result["next"]


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
    if not {"id", "client_id", "client_secret", "pem", "slug"} <= data.keys():
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


@dataclass(frozen=True)
class GitHubApp:
    """A converted App's credentials. Held in memory between the browser step and the Secret write."""

    app_id: int
    slug: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    pem: str = field(repr=False)


def install_url(host: str, slug: str) -> str:
    return f"https://{host}/{'apps' if host == 'github.com' else 'github-apps'}/{slug}/installations/new"  # GHES path


def open_browser(url: str, *, no_browser: bool = False) -> None:
    """WSL has no Linux browser and xdg-open's failure is terminal noise, so it goes through Windows' explorer.exe."""
    if no_browser:
        return
    if "microsoft" in platform.release().lower():
        if explorer := shutil.which("explorer.exe"):
            subprocess.Popen([explorer, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        return
    webbrowser.open(url)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt(app_id: int | str, pem: str, *, now: float | None = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(time.time() if now is None else now)
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in ({"alg": "RS256", "typ": "JWT"}, {"iat": issued - 60, "exp": issued + 540, "iss": str(app_id)})
    )
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubConnectError("the stored private key is not an RSA key")
    return f"{signing_input}.{_b64url(key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()))}"


def app_installed(*, host: str, organization: str, app_id: int | str, pem: str) -> bool | None:
    """Whether the App is installed on the organization; None when GitHub cannot say."""
    try:
        request = urllib.request.Request(
            f"{api_base(host)}/orgs/{urllib.parse.quote(organization, safe='')}/installation",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {app_jwt(app_id, pem)}"},
        )
        with urllib.request.urlopen(request, timeout=15):  # noqa: S310 - https URL built above
            return True
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except Exception:  # noqa: BLE001 - any other failure (network, a malformed key) means unknown
        return None


def create_app(context: Any, *, no_browser: bool = False) -> GitHubApp:
    """Run the manifest flow for spec.identity.github and return the converted App without writing it anywhere."""
    from olf import log

    topology = context.topology
    access = topology.access
    github = topology.identity.github if topology.identity else None
    if github is None or access is None:
        raise GitHubConnectError("openlakeforge.yaml has no spec.identity.github (issuer: github); nothing to connect")
    host = github.host or "github.com"
    scheme = "http" if access.issuer == "none" else "https"
    created: list[GitHubApp] = []

    def manifest_for(redirect_url: str) -> dict[str, Any]:
        return build_manifest(
            name=f"openlakeforge-{topology.profile_name[:13]}-{secrets.token_hex(3)}",  # App names are unique on GitHub
            callback_url=f"{scheme}://auth.{access.base_domain}/callback",
            redirect_url=redirect_url,
            homepage=f"{scheme}://{access.base_domain}",
        )

    def open_url(url: str) -> None:
        # Always print it: webbrowser.open reports success when xdg-open merely starts.
        log.info(
            f"Confirm the GitHub App in your browser; the deploy waits up to {TIMEOUT_SECONDS // 60} minutes "
            f"for this and then runs unattended: {url}"
        )
        open_browser(url, no_browser=no_browser)

    def on_code(code: str) -> str:
        data = convert_code(code, host=host)
        created.append(GitHubApp(data["id"], data["slug"], data["client_id"], data["client_secret"], data["pem"]))
        return install_url(host, data["slug"])

    wait_for_code(
        host=host, organization=github.organization, manifest_for=manifest_for, open_url=open_url, on_code=on_code
    )
    return created[0]


def _kube(context: Any) -> dict[str, Any]:
    return {
        "context": context.kube_context,
        "kubeconfig": context.paths.kubeconfig_path,
        "env": context.command_env(base=os.environ),
    }


def store_app(context: Any, app: GitHubApp, kubectl: Any) -> None:
    from olf import log

    ref, namespace = context.topology.identity.github.oauth_app_secret_ref, context.shared_namespace
    kubectl.apply_from_manifest(
        secret_manifest(
            name=ref.name,
            namespace=namespace,
            values={
                ref.client_id_key: app.client_id,
                ref.client_secret_key: app.client_secret,
                "private-key": app.pem,
                "app-id": str(app.app_id),
                "app-slug": app.slug,
            },
        ),
        **_kube(context),
    )
    log.step(f"Stored the GitHub App credentials in Secret '{ref.name}' (namespace '{namespace}').")


def connect_github(context: Any, *, no_browser: bool = False) -> str:
    """Create the GitHub App for spec.identity.github, store it as the Secret `oauth_app_secret_ref` names, and
    return the install link. Used by `olf github connect`; `olf deploy` runs `create_app` before the cluster exists."""
    from olf import log
    from olf.deployment.engine import Toolkit
    from olf.deployment.kube_ops import namespace_exists

    namespace = context.shared_namespace
    kube = _kube(context)
    tools = Toolkit.default(environ=kube["env"])
    if not namespace_exists(tools.kubectl, namespace, **kube):
        raise GitHubConnectError(
            f"namespace '{namespace}' does not exist; run olf deploy once (it creates it), then re-run this"
        )
    app = create_app(context, no_browser=no_browser)
    store_app(context, app, tools.kubectl)
    github = context.topology.identity.github
    install = install_url(github.host or "github.com", app.slug)
    log.info(f"Install the app on {github.organization}: {install}")
    return install
