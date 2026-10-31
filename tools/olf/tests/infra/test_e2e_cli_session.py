"""The e2e's `olf login` → `whoami` → `logout` check, against a fake sign-in."""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from olf.infra.e2e import _cli_session as cli
from olf.infra.e2e._shell import E2EError
from olf.platform import session as sessions

IDENTITY = {"issuer_url": "https://auth.olf.localhost", "clients": {"olf-cli": {"client_id": "olf-cli"}}}
CFG: Any = SimpleNamespace(platform_namespace="olf-system")
WHOAMI = "https://olf.localhost/api/v1/whoami"


def _jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


TOKEN = _jwt({"exp": int(time.time()) + 300, "email": "de@example.com", "groups": ["acme:data-engineers"]})


class SignIn:
    """`login.login` as it behaves: announce the URL, wait for the browser to reach the callback, store a session."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.urls: list[str] = []

    def login(self, instance: sessions.Instance, *, open_browser: bool, announce: Any) -> sessions.Session:
        assert not open_browser
        announce("Open this URL in a browser to sign in:\nhttps://auth.olf.localhost/auth?client_id=olf-cli")
        assert self.reached.wait(5)
        session = sessions.Session(instance.issuer, TOKEN, "", "")
        sessions.save(instance, session)
        return session

    def browser(self) -> Any:
        sign_in = self

        class Browser:
            def mount(self, prefix: str, adapter: Any) -> None:
                assert prefix == "https://olf-e2e-github.olf-system.svc"

            def get(self, url: str, **_: Any) -> Any:
                sign_in.urls.append(url)
                sign_in.reached.set()
                return SimpleNamespace(url="http://127.0.0.1:5000/callback?code=c&state=s")

        return Browser()


@pytest.fixture
def sign_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SignIn:
    monkeypatch.setenv("OLF_HOME", str(tmp_path / "operator"))
    fake = SignIn()
    monkeypatch.setattr(cli.login_flow, "login", fake.login)
    monkeypatch.setattr(cli.requests, "Session", fake.browser)
    monkeypatch.setattr(cli, "stub_host", lambda cfg: "olf-e2e-github.olf-system.svc")
    return fake


def _api(role: str, status: int = 200) -> Any:
    def get(url: str, headers: dict[str, str], **_: Any) -> Any:
        assert url == WHOAMI and headers["Authorization"] == f"Bearer {TOKEN}"
        return SimpleNamespace(status_code=status, json=lambda: {"role": role}, text="")

    return get


def test_login_whoami_and_logout_leave_no_session_and_never_touch_the_operators(
    sign_in: SignIn, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.requests, "get", _api("data-engineer"))

    cli.check_cli_session(CFG, IDENTITY, "data-engineer", "olf-e2e-de", WHOAMI)

    assert sign_in.urls == ["https://auth.olf.localhost/auth?client_id=olf-cli"]
    assert not (tmp_path / "operator").exists()


def test_the_instance_is_the_one_a_dex_profile_resolves() -> None:
    assert cli.cli_instance(IDENTITY) == sessions.Instance(
        "olf.localhost", "https://auth.olf.localhost", "olf-cli", True
    )


def test_a_stored_session_the_api_does_not_accept_fails(sign_in: SignIn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.requests, "get", _api("analyst"))

    with pytest.raises(E2EError, match="with the stored session"):
        cli.check_cli_session(CFG, IDENTITY, "data-engineer", "olf-e2e-de", WHOAMI)


def test_a_logout_that_leaves_the_session_fails(sign_in: SignIn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sessions, "delete", lambda instance: True)

    with pytest.raises(E2EError, match="after olf logout still shows a session"):
        cli.check_cli_session(CFG, IDENTITY, "data-engineer", "olf-e2e-de", None)


def test_a_login_that_fails_is_reported(monkeypatch: pytest.MonkeyPatch, sign_in: SignIn) -> None:
    def refused(instance: sessions.Instance, *, open_browser: bool, announce: Any) -> sessions.Session:
        announce("Open this URL in a browser to sign in:\nhttps://auth.olf.localhost/auth")
        assert sign_in.reached.wait(5)
        raise sessions.PlatformError("the issuer refused the sign-in: access_denied")

    monkeypatch.setattr(cli.login_flow, "login", refused)

    with pytest.raises(E2EError, match="access_denied"):
        cli.check_cli_session(CFG, IDENTITY, "data-engineer", "olf-e2e-de", None)
