"""Register the GitHub App that Dex signs people in with, through GitHub's manifest flow.

GitHub has no API to create an OAuth App, but it creates a GitHub App from a manifest the operator
confirms in the browser. A GitHub App's client id and secret drive Dex's github connector the same way
(ADR 0014). No credential is printed or logged here: the conversion response goes straight into the Secret.
"""

from __future__ import annotations

import base64
import html
import json
import secrets
import time
import urllib.parse
import urllib.request
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
