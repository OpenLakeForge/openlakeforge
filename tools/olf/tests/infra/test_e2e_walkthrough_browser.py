"""`sign_in` in a real Chromium, against a local server playing the perimeter, Dex and the Portal.

Playwright is not an `olf` dependency: the `browser` dependency group installs it, and the checks job that syncs the
group installs Chromium and sets OLF_REQUIRE_PLAYWRIGHT, so there a missing browser fails instead of skipping.
"""

from __future__ import annotations

import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from olf.infra.e2e import _walkthrough as walkthrough

sync_api = pytest.importorskip("playwright.sync_api")

STUB = "olf-e2e-github.olf-system.svc"
NOWHERE = "elsewhere.olf-e2e.invalid"
PORTAL_HOST, DEX_HOST = "olf.localhost", "auth.olf.localhost"
LOGIN, STATE = "olf-e2e-journey-x", "state-1"
SESSION = "olf-e2e-session=1"


class Perimeter(ThreadingHTTPServer):
    """The sign-in's redirect chain as the stack serves it: the Portal sends a browser without a session to Dex, Dex on
    to its GitHub connector's authorize page on the stub, and Dex's callback, given the stub's code, back to the Portal
    through a page that sets the session. Every request is kept as `<host> <path>`."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _PerimeterHandler)
        self.seen: list[str] = []

    def origin(self, host: str) -> str:
        return f"http://{host}:{self.server_address[1]}"


class _PerimeterHandler(BaseHTTPRequestHandler):
    server: Perimeter

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        host = (self.headers["Host"] or "").split(":")[0]
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        self.server.seen.append(f"{host} {url.path}")
        portal, dex = self.server.origin(PORTAL_HOST), self.server.origin(DEX_HOST)
        if host == PORTAL_HOST and url.path == "/monitoring" and SESSION not in (self.headers["Cookie"] or ""):
            return self._redirect(f"{dex}/auth?{urllib.parse.urlencode({'rd': self.path})}")
        if host == DEX_HOST and url.path == "/auth":
            return self._redirect(f"{dex}/auth/github?{url.query}")
        if host == DEX_HOST and url.path == "/auth/github":
            back = urllib.parse.urlencode({"redirect_uri": f"{dex}/callback", "state": STATE, "scope": "read:org"})
            return self._redirect(f"{self.server.origin(STUB)}/login/oauth/authorize?{back}")
        if host == DEX_HOST and url.path == "/callback":
            if query.get("code") != [LOGIN] or query.get("state") != [STATE]:
                return self._answer(400, "the callback lacks the stub's code or Dex's state")
            return self._redirect(f"{portal}/oauth2/callback")
        if host == PORTAL_HOST and url.path == "/oauth2/callback":
            return self._redirect(f"{portal}/monitoring?stage=dev", cookie=f"{SESSION}; Path=/")
        if host == PORTAL_HOST and url.path == "/monitoring":
            return self._answer(200, "<h1>Monitoring</h1>")
        return self._answer(404, "not part of the sign-in")

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _answer(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def perimeter() -> Iterator[Perimeter]:
    server = Perimeter()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def page(perimeter: Perimeter) -> Iterator[Any]:
    origins = [perimeter.origin(PORTAL_HOST), perimeter.origin(DEX_HOST)]
    # The resolver rules the walk launches with; no proxy, so the stub's name is resolved, and refused, by Chromium.
    args = [*walkthrough.resolver_rules(origins, {}, unresolved=[STUB, NOWHERE]), "--no-proxy-server"]
    with sync_api.sync_playwright() as driver:
        browser = driver.chromium.launch(args=args)
        try:
            tab = browser.new_context().new_page()
            tab.set_default_timeout(10_000)
            yield tab
        finally:
            browser.close()


def test_sign_in_follows_the_failed_stub_hop_back_to_dex_and_lands_on_the_page(perimeter: Perimeter, page: Any) -> None:
    overview = f"{perimeter.origin(PORTAL_HOST)}/monitoring?stage=dev"
    walkthrough.sign_in(page, overview, LOGIN, STUB, sync_api.Error)
    assert page.url == overview
    assert page.get_by_role("heading", level=1).inner_text() == "Monitoring"
    assert perimeter.seen == [
        f"{PORTAL_HOST} /monitoring",
        f"{DEX_HOST} /auth",
        f"{DEX_HOST} /auth/github",
        f"{DEX_HOST} /callback",
        f"{PORTAL_HOST} /oauth2/callback",
        f"{PORTAL_HOST} /monitoring",
    ]

    perimeter.seen.clear()
    walkthrough.sign_in(page, overview, LOGIN, STUB, sync_api.Error)
    assert page.url == overview and perimeter.seen == [f"{PORTAL_HOST} /monitoring"]


def test_sign_in_raises_a_navigation_that_fails_short_of_the_stub(page: Any) -> None:
    with pytest.raises(sync_api.Error, match="ERR_NAME_NOT_RESOLVED"):
        walkthrough.sign_in(page, f"http://{NOWHERE}/monitoring", LOGIN, STUB, sync_api.Error)
