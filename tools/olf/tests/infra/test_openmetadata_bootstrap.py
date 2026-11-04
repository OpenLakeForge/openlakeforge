"""The OpenMetadata auth bootstrap against a stateful fake server, with Secrets held in a dict."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra import k8s
from olf.infra.openmetadata import _auth, _bootstrap

EMAIL = "admin@open-metadata.org"


class FakeServer(ThreadingHTTPServer):
    """Login, changePassword, users, generateToken and token: the surface the bootstrap uses."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.password = "admin"
        self.sso = False  # basic login answers 403
        self.healthy = True
        self.fernet = False
        self.bot_admin = True
        self.probe_status: int | None = None
        self.users: dict[str, dict] = {"ingestion-bot": {"id": "id-ingestion-bot", "isAdmin": False}}
        self.tokens: dict[str, str] = {}  # user id -> JWT
        self.calls: list[tuple[str, str]] = []

    def user_for(self, bearer: str) -> dict | None:
        if bearer == "admin-jwt":
            return {"id": "id-admin", "isAdmin": True}
        for user_id, token in self.tokens.items():
            if token == bearer:
                user = next(u for u in self.users.values() if u["id"] == user_id)
                return {**user, "isAdmin": user["isAdmin"] and self.bot_admin}
        return None


class _Handler(BaseHTTPRequestHandler):
    server: FakeServer

    def log_message(self, *_args: object) -> None:
        pass

    def _reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self) -> None:
        srv, path = self.server, self.path
        srv.calls.append((self.command, path))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if path == "/api/v1/system/config/jwks":
            return self._reply(200 if srv.healthy else 503, {})
        if path == "/api/v1/users/login":
            if srv.sso:
                return self._reply(403, {})
            if base64.b64decode(body["password"]).decode() == srv.password:
                return self._reply(200, {"accessToken": "admin-jwt"})
            return self._reply(401, {})
        user = srv.user_for(bearer)
        if user is None:
            return self._reply(401, {})
        if path == "/api/v1/users/loggedInUser":
            return self._reply(srv.probe_status or 200, user)
        if path == "/api/v1/users/changePassword":
            srv.password = body["newPassword"]
            return self._reply(200, {})
        if path == "/api/v1/users":
            srv.users[body["name"]] = {"id": f"id-{body['name']}", "isAdmin": body["isAdmin"]}
            return self._reply(201, {"id": f"id-{body['name']}"})
        if path == "/api/v1/bots":
            return self._reply(201, {})
        if path.startswith("/api/v1/users/name/"):
            found = srv.users.get(path.rsplit("/", 1)[1])
            return self._reply(200 if found else 404, found or {})
        if path.startswith("/api/v1/users/generateToken/"):
            user_id = path.rsplit("/", 1)[1]
            srv.tokens[user_id] = f"jwt-{user_id}-{len(srv.tokens)}"
            return self._reply(200, {})
        if path.startswith("/api/v1/users/token/"):
            token = srv.tokens[path.rsplit("/", 1)[1]]
            return self._reply(200, {"JWTToken": f"fernet:{token}" if srv.fernet else token})
        self._reply(404, {})

    do_GET = do_PUT = do_POST = _handle  # noqa: N815 - BaseHTTPRequestHandler API


@pytest.fixture
def om() -> Iterator[FakeServer]:
    server = FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], dict[str, str]]:
    """Kubernetes Secrets keyed by (namespace, name)."""
    secrets: dict[tuple[str, str], dict[str, str]] = {}

    def secret_value(name: str, key: str, namespace: str, **_kwargs: object) -> str:
        if (namespace, name) not in secrets:
            if _kwargs.get("missing_ok"):
                return ""
            raise k8s.KubectlError("NotFound")
        return secrets[(namespace, name)].get(key, "")

    def write_secret(name: str, namespace: str, data: dict[str, str], *, overwrite: bool = True) -> None:
        if not overwrite and (namespace, name) in secrets:
            raise k8s.KubectlError("AlreadyExists")
        secrets[(namespace, name)] = dict(data)

    monkeypatch.setattr(k8s, "secret_value", secret_value)
    monkeypatch.setattr(k8s, "write_secret", write_secret)
    monkeypatch.setattr(_bootstrap, "HEALTH_DELAY", 0)
    return secrets


def _run(om: FakeServer, *, sso: bool = False) -> None:
    _bootstrap.bootstrap_auth(om.url, "ns", email=EMAIL, sso=sso)


def test_first_run_changes_the_default_password_and_stores_the_ingestion_token(om: FakeServer, store: dict) -> None:
    _run(om)

    password = store[("ns", _auth.ADMIN_SECRET)]["admin-password"]
    assert om.password == password
    assert len(password) == 28 and password.endswith("Aa1!")
    assert store[("ns", _auth.INGESTION_BOT_SECRET)] == {_auth.INGESTION_BOT_JWT_KEY: om.tokens["id-ingestion-bot"]}
    assert ("ns", _auth.AUTOMATION_BOT) not in store  # no SSO, no bot


def test_rerun_reuses_the_stored_password_and_never_changes_it(om: FakeServer, store: dict) -> None:
    _run(om)
    om.calls.clear()

    _run(om)

    assert ("PUT", "/api/v1/users/changePassword") not in om.calls
    assert om.password == store[("ns", _auth.ADMIN_SECRET)]["admin-password"]


def test_a_stored_password_that_no_longer_matches_fails_loudly(om: FakeServer, store: dict) -> None:
    store[("ns", _auth.ADMIN_SECRET)] = {"admin-password": "stale"}
    om.password = "someone-elses"

    with pytest.raises(OpenMetadataError, match="Failed to obtain admin JWT"):
        _run(om)


def test_sso_creates_the_automation_bot_then_later_runs_authenticate_with_it(om: FakeServer, store: dict) -> None:
    _run(om, sso=True)

    stored = store[("ns", _auth.AUTOMATION_BOT)]
    assert stored == {_auth.AUTOMATION_JWT_KEY: om.tokens["id-olf-automation"]}
    assert om.users["olf-automation"]["isAdmin"]

    om.sso = True  # basic login is now refused
    om.calls.clear()
    _run(om, sso=True)

    assert ("POST", "/api/v1/users/login") not in om.calls
    assert ("PUT", "/api/v1/users") not in om.calls
    assert store[("ns", _auth.AUTOMATION_BOT)] == stored
    assert store[("ns", _auth.INGESTION_BOT_SECRET)][_auth.INGESTION_BOT_JWT_KEY] == om.tokens["id-ingestion-bot"]


def test_a_stale_automation_token_falls_back_to_basic_login_and_is_replaced(om: FakeServer, store: dict) -> None:
    _run(om, sso=True)
    store[("ns", _auth.AUTOMATION_BOT)] = {_auth.AUTOMATION_JWT_KEY: "stale"}  # a rebuilt database

    _run(om, sso=True)

    assert store[("ns", _auth.AUTOMATION_BOT)][_auth.AUTOMATION_JWT_KEY] == om.tokens["id-olf-automation"]


def test_a_bot_that_is_not_an_admin_is_rejected_and_nothing_is_stored(om: FakeServer, store: dict) -> None:
    om.bot_admin = False

    with pytest.raises(OpenMetadataError, match="not an administrator"):
        _run(om, sso=True)

    assert ("ns", _auth.AUTOMATION_BOT) not in store


def test_an_encrypted_token_is_rejected(om: FakeServer, store: dict) -> None:
    om.fernet = True

    with pytest.raises(OpenMetadataError, match="bearer JWT for ingestion-bot"):
        _run(om)

    assert ("ns", _auth.INGESTION_BOT_SECRET) not in store


def test_the_command_bootstraps_the_shared_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib

    from typer.testing import CliRunner

    from olf.cli import app
    from olf.infra.commands import runtime

    calls: list[tuple] = []
    monkeypatch.setattr(runtime, "provider_contract_environment", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setenv("OPENLAKEFORGE_SHARED_NAMESPACE", "olf-shared")
    monkeypatch.setattr(_bootstrap, "bootstrap_auth", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = CliRunner().invoke(app, ["infra", "openmetadata", "bootstrap", "--om-url", "http://om:8585", "--sso"])

    assert result.exit_code == 0, result.output
    assert calls == [(("http://om:8585", "olf-shared"), {"email": EMAIL, "sso": True})]


def test_an_unhealthy_server_times_out(om: FakeServer, store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    om.healthy = False
    monkeypatch.setattr(_bootstrap, "HEALTH_ATTEMPTS", 2)

    with pytest.raises(OpenMetadataError, match="did not become healthy"):
        _run(om)


def test_a_rerun_keeps_the_token_already_copied_into_stage_namespaces(om: FakeServer, store: dict) -> None:
    _run(om)
    shared = ("ns", _auth.INGESTION_BOT_SECRET)
    stage = ("olf-dev", _auth.INGESTION_BOT_SECRET)
    store[stage] = dict(store[shared])
    original = store[shared][_auth.INGESTION_BOT_JWT_KEY]
    om.calls.clear()

    _run(om)

    assert store[shared] == store[stage] == {_auth.INGESTION_BOT_JWT_KEY: original}
    assert ("PUT", "/api/v1/users/generateToken/id-ingestion-bot") not in om.calls
    assert (
        OpenMetadataClient(om.url, token=original).request("GET", "/api/v1/users/loggedInUser")["id"]
        == "id-ingestion-bot"
    )


@pytest.mark.parametrize("stored", ["expired", "admin-jwt"])
def test_a_stale_or_different_users_ingestion_credential_is_replaced(om: FakeServer, store: dict, stored: str) -> None:
    store[("ns", _auth.INGESTION_BOT_SECRET)] = {_auth.INGESTION_BOT_JWT_KEY: stored}

    _run(om)

    token = store[("ns", _auth.INGESTION_BOT_SECRET)][_auth.INGESTION_BOT_JWT_KEY]
    assert token != stored
    assert (
        OpenMetadataClient(om.url, token=token).request("GET", "/api/v1/users/loggedInUser")["id"] == "id-ingestion-bot"
    )


def test_an_ingestion_token_is_not_rotated_when_verification_is_unavailable(om: FakeServer, store: dict) -> None:
    _run(om)
    original = dict(store[("ns", _auth.INGESTION_BOT_SECRET)])
    om.calls.clear()
    om.probe_status = 503

    with pytest.raises(ServiceClientError):
        _run(om)

    assert store[("ns", _auth.INGESTION_BOT_SECRET)] == original
    assert ("PUT", "/api/v1/users/generateToken/id-ingestion-bot") not in om.calls
