from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import HTTPServer

import pytest
import requests

from olf.e2e import github_stub
from olf.e2e._identity_github import sign_in_with_stub

USERS = {"alice": ["Acme:analysts"], "bob": ["Acme:unmapped"]}
AUTHORIZE = "https://stub.ns.svc/login/oauth/authorize?state=s&redirect_uri=https://auth.olf.localhost/callback"


@pytest.fixture
def stub() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), github_stub.make_handler(USERS))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_code_is_the_login_and_the_token_reads_back_as_that_user(stub: str) -> None:
    token = requests.post(f"{stub}/login/oauth/access_token", data={"code": "alice"}, timeout=5).json()
    auth = {"Authorization": f"Bearer {token['access_token']}"}
    assert requests.get(f"{stub}/api/v3/user", headers=auth, timeout=5).json()["login"] == "alice"
    teams = requests.get(f"{stub}/api/v3/user/teams", headers=auth, timeout=5).json()
    assert teams == [{"name": "analysts", "slug": "analysts", "organization": {"login": "Acme"}}]
    assert requests.get(f"{stub}/api/v3/orgs/Acme/members/alice", headers=auth, timeout=5).status_code == 204


def test_an_unknown_code_or_token_is_refused(stub: str) -> None:
    assert requests.post(f"{stub}/login/oauth/access_token", data={"code": "mallory"}, timeout=5).status_code == 400
    assert requests.get(f"{stub}/api/v3/user", timeout=5).status_code == 401


def test_the_walk_answers_the_stub_hop_with_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    hops = {
        "https://olf.localhost": (302, "https://auth.olf.localhost/auth"),
        "https://auth.olf.localhost/auth": (302, AUTHORIZE),
        "https://auth.olf.localhost/callback?code=alice&state=s": (200, ""),
    }
    seen: list[str] = []

    def get(_self: requests.Session, url: str, **_kwargs: object) -> requests.Response:
        seen.append(url)
        status, location = hops[url]
        reply = requests.Response()
        reply.status_code = status
        reply.url = url
        if location:
            reply.headers["Location"] = location
        return reply

    monkeypatch.setattr(requests.Session, "get", get)
    _, last = sign_in_with_stub("https://olf.localhost", "alice", "stub.ns.svc")
    assert last.status_code == 200
    assert not any("stub.ns.svc" in url for url in seen)
