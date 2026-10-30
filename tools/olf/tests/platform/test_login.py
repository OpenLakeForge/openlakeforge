"""Platform CLI session: login, refresh, whoami and logout against a fake issuer."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import stat
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from olf.cli import app
from olf.platform import login as login_flow
from olf.platform import session as sessions

CLIENT = "olf-cli"


def _jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature-not-checked"


class FakeIssuer:
    """Discovery, `/auth` (redirects to the loopback with a code) and `/token` (checks PKCE)."""

    def __init__(self) -> None:
        self.challenges: dict[str, tuple[str, str]] = {}
        self.authorize_queries: list[dict[str, str]] = []
        self.refreshes = 0
        self.scopes_supported: list[str] | None = None
        self.id_token_lifetime = 300
        self.reject_refresh = False
        self.url = ""
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, body: dict[str, object]) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self) -> None:
                url = urllib.parse.urlsplit(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                if url.path == "/.well-known/openid-configuration":
                    self._json(
                        200,
                        {
                            "authorization_endpoint": f"{issuer.url}/auth?tenant=team",
                            "token_endpoint": f"{issuer.url}/token",
                            **({"scopes_supported": issuer.scopes_supported} if issuer.scopes_supported else {}),
                        },
                    )
                elif url.path == "/auth":
                    issuer.authorize_queries.append(query)
                    code = f"code-{len(issuer.challenges)}"
                    issuer.challenges[code] = (query.get("code_challenge", ""), query["redirect_uri"])
                    location = f"{query['redirect_uri']}?" + urllib.parse.urlencode(
                        {"code": code, "state": query["state"]}
                    )
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                form = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(
                        self.rfile.read(int(self.headers["Content-Length"])).decode()
                    ).items()
                }
                if form["grant_type"] == "refresh_token":
                    issuer.refreshes += 1
                    if issuer.reject_refresh or form["refresh_token"] != "refresh-1":
                        self._json(400, {"error": "invalid_grant"})
                        return
                    self._json(200, issuer.tokens())
                    return
                challenge, redirect = issuer.challenges.get(form["code"], ("", ""))
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                if (
                    base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge
                    or form["redirect_uri"] != redirect
                ):
                    self._json(400, {"error": "invalid_grant"})
                    return
                self._json(200, issuer.tokens())

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tokens(self) -> dict[str, object]:
        claims = {
            "preferred_username": "ada",
            "email": "ada@example.com",
            "groups": ["olf-analysts", "olf-viewers"],
            "exp": int(time.time()) + self.id_token_lifetime,
        }
        return {"id_token": _jwt(claims), "access_token": _jwt(claims), "refresh_token": "refresh-1"}


@pytest.fixture
def issuer() -> Iterator[FakeIssuer]:
    fake = FakeIssuer()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OLF_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


def _browser(monkeypatch: pytest.MonkeyPatch, *, follow: Callable[[str], None] | None = None) -> None:
    """A browser that follows the authorize URL, redirect included, from another thread."""

    def open_url(url: str) -> bool:
        threading.Thread(target=follow or (lambda target: requests.get(target, timeout=10)), args=(url,)).start()
        return True

    monkeypatch.setattr("webbrowser.open", open_url)


def _instance(issuer: FakeIssuer) -> sessions.Instance:
    return sessions.Instance("olf.example.com", issuer.url, CLIENT)


def test_login_stores_a_pkce_session_in_a_private_file(issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> None:
    _browser(monkeypatch)
    instance = _instance(issuer)

    login_flow.login(instance, open_browser=True, announce=lambda _: None)

    query = issuer.authorize_queries[0]
    assert query["code_challenge_method"] == "S256"
    assert query["client_id"] == CLIENT
    assert query["tenant"] == "team"  # the endpoint's own query survives
    assert query["redirect_uri"].startswith("http://127.0.0.1:") and query["redirect_uri"].endswith("/callback")
    assert set(query["scope"].split()) == {"openid", "email", "profile", "groups", "offline_access"}
    path = sessions.session_path(instance)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sessions.active_session(instance).refresh_token == "refresh-1"


def test_login_tightens_the_mode_of_an_existing_session_file(
    issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _instance(issuer)
    path = sessions.session_path(instance)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0o644)
    _browser(monkeypatch)

    login_flow.login(instance, open_browser=True, announce=lambda _: None)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_login_completes_with_a_blocking_browser_and_an_idle_connection(
    issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def foreground_browser(url: str) -> bool:
        redirect = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["redirect_uri"][0]
        port = urllib.parse.urlsplit(redirect).port
        with socket.create_connection(("127.0.0.1", port or 0)):  # sends nothing
            requests.get(url, timeout=10)  # returns only once the callback was served
        return True

    monkeypatch.setattr("webbrowser.open", foreground_browser)

    login_flow.login(_instance(issuer), open_browser=True, announce=lambda _: None)

    assert sessions.active_session(_instance(issuer)).refresh_token == "refresh-1"


def test_login_asks_an_external_issuer_only_for_scopes_it_advertises(
    issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer.scopes_supported = ["openid", "profile", "email", "offline_access"]
    _browser(monkeypatch)

    login_flow.login(_instance(issuer), open_browser=True, announce=lambda _: None)

    assert set(issuer.authorize_queries[0]["scope"].split()) == {"openid", "profile", "email", "offline_access"}


def test_no_browser_prints_the_url_and_never_opens_one(issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("opened a browser"))
    printed: list[str] = []

    def announce(message: str) -> None:
        printed.append(message)
        url = message.split("\n")[-1]
        threading.Thread(target=lambda: requests.get(url, timeout=10)).start()

    login_flow.login(_instance(issuer), open_browser=False, announce=announce)

    assert printed[0].startswith("Open this URL") and "code_challenge_method=S256" in printed[0]


def test_login_rejects_a_response_with_the_wrong_state(issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> None:
    def forged(url: str) -> None:
        redirect = requests.get(url, allow_redirects=False, timeout=10).headers["Location"]
        requests.get(redirect.replace("state=", "state=forged"), timeout=10)

    _browser(monkeypatch, follow=forged)

    with pytest.raises(sessions.PlatformError, match="did not match"):
        login_flow.login(_instance(issuer), open_browser=True, announce=lambda _: None)
    assert not sessions.session_path(_instance(issuer)).exists()


def test_expired_id_token_is_refreshed_without_a_secret(issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> None:
    _browser(monkeypatch)
    instance = _instance(issuer)
    issuer.id_token_lifetime = -10
    login_flow.login(instance, open_browser=True, announce=lambda _: None)
    issuer.id_token_lifetime = 300

    refreshed = sessions.active_session(instance)

    assert issuer.refreshes == 1
    assert sessions.claims(refreshed.id_token)["exp"] > time.time()
    assert sessions.active_session(instance) == refreshed
    assert issuer.refreshes == 1


def test_a_refused_refresh_asks_for_a_new_login(issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> None:
    _browser(monkeypatch)
    instance = _instance(issuer)
    issuer.id_token_lifetime = -10
    login_flow.login(instance, open_browser=True, announce=lambda _: None)
    issuer.reject_refresh = True

    with pytest.raises(sessions.PlatformError, match="expired; run `olf login`"):
        sessions.active_session(instance)


def test_no_session_says_not_logged_in(issuer: FakeIssuer) -> None:
    with pytest.raises(sessions.PlatformError, match="not logged in"):
        sessions.active_session(_instance(issuer))


PROFILE = """\
apiVersion: openlakeforge.io/v1alpha1
kind: DeploymentProfile
metadata:
  name: fixture
spec:
  provider: {type: local}
  preset: slim
  stages: {dev: {enabled: true}}
  access: {base_domain: olf.example.com}
  identity:
    issuer: external
    issuer_url: https://login.example.com
    role_claim: groups
    role_mapping: {platform-admin: [admins]}
    client_ids: {olf-cli: cli-123}
"""


def _profile(tmp_path: Path, text: str = PROFILE) -> str:
    path = tmp_path / "openlakeforge.yaml"
    path.write_text(text)
    return str(path)


def test_instance_comes_from_the_profile(tmp_path: Path) -> None:
    external = sessions.resolve_instance(_profile(tmp_path))
    assert external == sessions.Instance("olf.example.com", "https://login.example.com", "cli-123")

    github = Path(__file__).parents[1] / "fixtures/profiles/valid_github_identity.yaml"
    dex = sessions.resolve_instance(str(github))
    assert (dex.issuer, dex.client_id) == ("https://auth.olf.localhost", CLIENT)


def test_plain_http_access_with_identity_still_signs_in(tmp_path: Path) -> None:
    github = Path(__file__).parents[1] / "fixtures/profiles/valid_github_identity.yaml"
    profile = tmp_path / "openlakeforge.yaml"
    profile.write_text(github.read_text().replace("access: {}", "access: {issuer: none}"))

    assert sessions.resolve_instance(str(profile)).issuer == "http://auth.olf.localhost"


@pytest.mark.parametrize("access", ["", "  access: {issuer: none}\n"])
def test_a_deployment_without_sign_in_points_at_olf_infra(tmp_path: Path, access: str) -> None:
    profile = _profile(tmp_path, PROFILE.split("  access:")[0] + access)
    result = CliRunner().invoke(app, ["login", "--profile-file", profile])

    assert result.exit_code == 1
    assert "no sign-in" in result.output and "olf infra" in result.output


def test_whoami_and_logout_never_print_a_token(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    instance = sessions.resolve_instance(profile)
    id_token = _jwt(
        {"preferred_username": "ada", "email": "ada@example.com", "groups": ["olf-analysts"], "exp": time.time() + 600}
    )
    sessions.save(instance, sessions.Session(instance.issuer, id_token, "access-secret", "refresh-secret"))
    runner = CliRunner()

    who = runner.invoke(app, ["whoami", "--profile-file", profile])

    assert who.exit_code == 0
    for expected in ("olf.example.com", "https://login.example.com", "ada", "ada@example.com", "olf-analysts"):
        assert expected in who.output
    assert not {id_token, "access-secret", "refresh-secret"} & set(who.output.split())

    out = runner.invoke(app, ["logout", "--profile-file", profile])
    assert out.exit_code == 0 and not sessions.session_path(instance).exists()
    assert "not logged in" in runner.invoke(app, ["whoami", "--profile-file", profile]).output
    assert "Not logged in" in runner.invoke(app, ["logout", "--profile-file", profile]).output


def test_a_damaged_session_never_echoes_its_contents(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    path = sessions.session_path(sessions.resolve_instance(profile))
    path.parent.mkdir(parents=True)
    path.write_text('{"id_token": "not-a-jwt-secret"}')

    result = CliRunner().invoke(app, ["whoami", "--profile-file", profile])

    assert result.exit_code == 1 and "olf login" in result.output and "not-a-jwt-secret" not in result.output


def test_tls_failure_explains_the_local_ca(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise requests.exceptions.SSLError("self-signed certificate")

    monkeypatch.setattr(requests, "request", refuse)

    with pytest.raises(sessions.PlatformError, match="olf infra access trust"):
        sessions.discover(sessions.Instance("olf.example.com", "https://auth.olf.example.com", CLIENT))


def test_ssl_cert_file_is_honoured_for_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sessions.verify() is True
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/ca.crt")
    assert sessions.verify() == "/tmp/ca.crt"
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/bundle.crt")
    assert sessions.verify() == "/tmp/bundle.crt"


def test_a_localhost_issuer_is_reached_when_the_resolver_refuses_it(
    issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    real = socket.getaddrinfo

    def nxdomain(host: object, *args: object, **kwargs: object) -> object:
        if isinstance(host, str) and host.endswith(".localhost"):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", nxdomain)
    port = issuer.url.rsplit(":", 1)[1]
    instance = sessions.Instance("olf.localhost", f"http://auth.olf.localhost:{port}", CLIENT)

    assert "token_endpoint" in sessions.discover(instance)
    assert socket.getaddrinfo is nxdomain
