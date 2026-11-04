"""The browser-session check: a cookie-only whoami as olf-cli's audience, and a refused cross-origin write."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from olf.infra.e2e import _browser_session as browser_session
from olf.infra.e2e._shell import E2EError

IDENTITY = {"clients": {"olf-cli": {"client_id": "olf-cli"}}}
API = "https://olf.localhost/"


def _reply(status: int, body: Any = None, text: str = "") -> Any:
    return SimpleNamespace(status_code=status, json=lambda: body, text=text)


def _browser(whoami: Any, write: Any) -> Any:
    return SimpleNamespace(get=lambda *_a, **_k: whoami, post=lambda *_a, **_k: write)


GOOD_WHOAMI = _reply(200, {"role": "analyst", "audience": "olf-cli"})
GOOD_WRITE = _reply(403, text='{"detail":"cross-origin write refused"}')


def test_the_session_reads_as_olf_cli_and_a_cross_origin_write_is_refused() -> None:
    browser_session.check_browser_session(IDENTITY, API, "analyst", _browser(GOOD_WHOAMI, GOOD_WRITE))


@pytest.mark.parametrize(
    "whoami",
    [
        _reply(401, text="no bearer"),
        _reply(200, {"role": "viewer", "audience": "olf-cli"}),
        _reply(200, {"role": "analyst", "audience": "perimeter"}),
    ],
)
def test_a_session_that_the_api_does_not_accept_as_the_role_fails(whoami: Any) -> None:
    with pytest.raises(E2EError, match="whoami"):
        browser_session.check_browser_session(IDENTITY, API, "analyst", _browser(whoami, GOOD_WRITE))


@pytest.mark.parametrize("write", [_reply(202), _reply(403, text="Forbidden by the perimeter"), _reply(401)])
def test_a_cross_origin_write_must_be_refused_by_the_api_guard(write: Any) -> None:
    with pytest.raises(E2EError, match="POST from"):
        browser_session.check_browser_session(IDENTITY, API, "analyst", _browser(GOOD_WHOAMI, write))
