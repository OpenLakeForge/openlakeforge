from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from olf.e2e import _perimeter
from olf.e2e._shell import E2EError

ISSUER = "https://auth.olf.localhost/realms/openlakeforge"
ROUTES = {"shared/portal": "https://olf.localhost", "stage/dev/orchestration": "https://dagster-dev.olf.localhost"}
IDENTITY = {
    "issuer_url": ISSUER,
    "roles": {"grants": {"orchestration": {"data-engineer": "full"}, "reporting": {"analyst": "Gamma"}}},
}


def _reply(status: int, location: str = "", text: str = "") -> Any:
    return SimpleNamespace(status_code=status, headers={"Location": location}, text=text, url="https://x")


def test_a_route_is_granted_by_the_role_model_and_the_portal_to_every_role() -> None:
    assert _perimeter._grants(IDENTITY, "stage/dev/orchestration", "data-engineer")
    assert not _perimeter._grants(IDENTITY, "stage/dev/orchestration", "analyst")
    assert _perimeter._grants(IDENTITY, "shared/portal", "analyst")


def test_unauthenticated_requests_must_be_redirected_to_the_issuer_or_get_401(monkeypatch: pytest.MonkeyPatch) -> None:
    login = f"{ISSUER}/protocol/openid-connect/auth?client_id=perimeter"

    def answer(_url: str, headers: dict[str, str], **_kwargs: Any) -> Any:
        return _reply(401) if headers.get("Accept") == "application/json" else _reply(302, login)

    monkeypatch.setattr(_perimeter.requests, "get", answer)
    _perimeter.check_unauthenticated_requests(IDENTITY, list(ROUTES), ROUTES)

    monkeypatch.setattr(_perimeter.requests, "get", lambda *_a, **_k: _reply(200))
    with pytest.raises(E2EError, match="expected a redirect"):
        _perimeter.check_unauthenticated_requests(IDENTITY, list(ROUTES), ROUTES)


def test_a_denial_needs_the_status_the_heading_and_the_portal_link() -> None:
    page = f'<h1>{_perimeter.DENIAL_HEADING}</h1><a href="https://olf.localhost">Return</a>'
    _perimeter._require_denial(_reply(403, text=page), "analyst", "https://olf.localhost")

    for bad in (_reply(200, text=page), _reply(403, text="Forbidden"), _reply(403, text=page.replace("olf.", "x."))):
        with pytest.raises(E2EError, match="expected a 403"):
            _perimeter._require_denial(bad, "analyst", "https://olf.localhost")


def _signed_in(monkeypatch: pytest.MonkeyPatch, pages: dict[str, Any], direct: Any) -> None:
    browser = SimpleNamespace(cookies=SimpleNamespace(copy=dict), get=lambda url, **_kwargs: pages[url])
    monkeypatch.setattr(_perimeter, "_sign_in", lambda *_args: (browser, _reply(200)))
    monkeypatch.setattr(_perimeter.requests, "get", lambda *_args, **_kwargs: direct)


def test_a_denied_route_must_also_refuse_a_direct_api_call_with_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    denial = _reply(403, text=f'<h1>{_perimeter.DENIAL_HEADING}</h1><a href="https://olf.localhost">Return</a>')
    pages = {"https://olf.localhost": _reply(200), "https://dagster-dev.olf.localhost": denial}
    _signed_in(monkeypatch, pages, denial)
    _perimeter.check_role_decisions(IDENTITY, list(ROUTES), ROUTES, "analyst", "u", "p")

    _signed_in(monkeypatch, pages, _reply(200))
    with pytest.raises(E2EError, match="directly"):
        _perimeter.check_role_decisions(IDENTITY, list(ROUTES), ROUTES, "analyst", "u", "p")


def test_a_user_with_no_role_is_refused_at_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_perimeter, "_sign_in", lambda *_args: (None, _reply(200)))
    with pytest.raises(E2EError, match="no role"):
        _perimeter.check_role_decisions(IDENTITY, list(ROUTES), ROUTES, None, "u", "p")
