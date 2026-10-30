"""Authorization Code + PKCE login through a loopback redirect (ADR 0020)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from olf.platform.session import SCOPES, Instance, PlatformError, Session, discover, save, send

_TIMEOUT_SECONDS = 300


def _callback_server() -> tuple[ThreadingHTTPServer, dict[str, str]]:
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # an idle connection must not hold up the real callback

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/callback":
                self.send_error(404)
                return
            received.update({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and return to the terminal.\n")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format: str, *args: object) -> None:
            pass  # the default logs the request line, which carries the authorization code

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server, received


def login(instance: Instance, *, open_browser: bool, announce: Callable[[str], None]) -> Session:
    endpoints = discover(instance)
    server, received = _callback_server()
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    # Dex accepts a request without a challenge, so S256 is always sent.
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    endpoint = str(endpoints["authorization_endpoint"])
    url = f"{endpoint}{'&' if '?' in endpoint else '?'}" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": instance.client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    # Serving starts first: a foreground browser (lynx, a synchronous $BROWSER) blocks webbrowser.open.
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    opened = open_browser and webbrowser.open(url)
    announce("Waiting for the browser sign-in." if opened else f"Open this URL in a browser to sign in:\n{url}")
    serving.join(_TIMEOUT_SECONDS)
    server.shutdown()
    server.server_close()
    if not received:
        raise PlatformError("timed out waiting for the browser sign-in")
    if received.get("state") != state:
        raise PlatformError("the sign-in response did not match this login attempt")
    if "code" not in received:
        raise PlatformError(f"the issuer refused the sign-in: {received.get('error', 'no authorization code')}")

    try:
        response = send(
            "POST",
            str(endpoints["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": received["code"],
                "redirect_uri": redirect_uri,
                "client_id": instance.client_id,
                "code_verifier": verifier,
            },
            timeout=15,
        )
        tokens = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PlatformError(f"cannot complete the sign-in: {type(exc).__name__}") from exc
    if response.status_code != 200 or "id_token" not in tokens:
        raise PlatformError(f"the issuer refused the token request: {tokens.get('error', response.status_code)}")
    session = Session(
        instance.issuer, tokens["id_token"], tokens.get("access_token", ""), tokens.get("refresh_token", "")
    )
    save(instance, session)
    return session
