from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import HTTPServer

import pytest
import requests

from olf.infra.e2e import github_stub
from olf.infra.e2e._identity_github import StubAuthorize

USERS = {"alice": ["Acme:analysts"], "bob": ["Acme:unmapped"]}
AUTHORIZE = "https://stub.ns.svc/login/oauth/authorize?state=s&redirect_uri=https://auth.olf.localhost/callback"


@pytest.fixture
def stub() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), github_stub.make_handler(USERS))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_code_is_the_login_and_the_token_reads_back_as_that_user(stub: str) -> None:
    form = {"code": "alice", "client_id": "i", "client_secret": "s"}
    token = requests.post(f"{stub}/login/oauth/access_token", data=form, timeout=5).json()
    auth = {"Authorization": f"Bearer {token['access_token']}"}
    assert requests.get(f"{stub}/api/v3/user", headers=auth, timeout=5).json()["login"] == "alice"
    teams = requests.get(f"{stub}/api/v3/user/teams", headers=auth, timeout=5).json()
    assert teams == [{"name": "analysts", "slug": "analysts", "organization": {"login": "Acme"}}]
    assert requests.get(f"{stub}/api/v3/orgs/Acme/members/alice", headers=auth, timeout=5).status_code == 204


def test_an_unknown_code_or_token_is_refused(stub: str) -> None:
    token_url = f"{stub}/login/oauth/access_token"
    wrong_code = {"code": "mallory", "client_id": "i", "client_secret": "s"}
    assert requests.post(token_url, data=wrong_code, timeout=5).status_code == 400
    assert requests.post(token_url, data={"code": "alice"}, timeout=5).status_code == 401
    assert requests.get(f"{stub}/api/v3/user", timeout=5).status_code == 401


def test_every_authorize_redirect_in_the_session_is_answered_with_the_login() -> None:
    adapter = StubAuthorize("alice")
    for _ in range(2):  # a later sign-in, after the proxy cleared its cookie, is answered the same way
        request = requests.Request("GET", AUTHORIZE).prepare()
        reply = adapter.send(request)
        assert reply.headers["Location"] == "https://auth.olf.localhost/callback?code=alice&state=s"
