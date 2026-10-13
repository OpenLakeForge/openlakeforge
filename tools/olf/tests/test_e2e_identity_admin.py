from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import e2e_cfg

from olf.e2e import _identity_admin
from olf.e2e._shell import E2EError

_IDENTITY: dict[str, Any] = {
    "issuer_url": "https://auth.olf.localhost/realms/openlakeforge",
    "capabilities": {"admin_api": True},
    "admin": {"client_id": "olf-users", "secret_ref": {"name": "s", "key": "client-secret"}},
}
_BASE = "http://127.0.0.1:1"
_REALM = f"{_BASE}/admin/realms/openlakeforge"


class _Keycloak:
    """The responses a correctly limited `olf-users` client sees; a test breaks one."""

    def __init__(self) -> None:
        self.overrides: dict[str, Any] = {}

    def response(self, status: int, body: Any = None) -> SimpleNamespace:
        return SimpleNamespace(
            status_code=status, json=lambda: body, raise_for_status=lambda: None
        )

    def token(self, _url: str, data: dict[str, str], **_kwargs: Any) -> SimpleNamespace:
        if data["client_secret"] != "referenced":
            return self.response(401)
        if data["grant_type"] == "password":
            return self.override("password", self.response(401))
        return self.response(200, {"access_token": "t"})

    def override(self, key: str, default: Any) -> Any:
        return self.overrides.get(key, default)

    def request(self, method: str, url: str, **_kwargs: Any) -> SimpleNamespace:
        if url.endswith(("/users", "/groups")) and "master" not in url:
            return self.override(url.rsplit("/", 1)[1], self.response(200, [{"username": "alice"}]))
        return self.override("forbidden", self.response(403))

    def get(self, url: str, **kwargs: Any) -> SimpleNamespace:
        return self.request("GET", url, **kwargs)


def _admin_session(realm: dict[str, Any], events: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        get=lambda url, **_kwargs: SimpleNamespace(json=lambda: events if url.endswith("admin-events") else realm)
    )


_RECORDING = {"adminEventsEnabled": True, "adminEventsDetailsEnabled": False}


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keycloak: _Keycloak, **session: Any) -> None:
    monkeypatch.setattr(_identity_admin.k8s, "secret_value", lambda *_args, **_kwargs: "referenced")
    monkeypatch.setattr(_identity_admin.requests, "post", keycloak.token)
    monkeypatch.setattr(_identity_admin.requests, "get", keycloak.get)
    monkeypatch.setattr(_identity_admin.requests, "request", keycloak.request)
    admin = _admin_session(session.get("realm", _RECORDING), session.get("events", [{"operationType": "CREATE"}]))
    _identity_admin.check_admin_client(e2e_cfg(tmp_path), _IDENTITY, _BASE, admin)  # type: ignore[arg-type]


def test_a_limited_client_that_authenticates_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _run(monkeypatch, tmp_path, _Keycloak())


_OK = (200, [])
_SERVICE_ACCOUNT = (200, [{"username": "service-account-olf-users"}])


@pytest.mark.parametrize(
    ("overrides", "session", "match"),
    [
        ({"forbidden": _OK}, {}, "must not write the realm settings"),
        ({"forbidden": (200, {"smtpServer": {}})}, {}, "realm representation carries them"),
        ({"users": _SERVICE_ACCOUNT}, {}, "service account"),
        ({"password": _OK}, {}, "not be a login client"),
        ({}, {"realm": {"adminEventsEnabled": False}}, "record admin events"),
        ({}, {"realm": {**_RECORDING, "adminEventsDetailsEnabled": True}}, "record admin events"),
        ({}, {"events": []}, "no admin event"),
        ({}, {"events": [{"representation": '{"credentials": []}'}]}, "carries a representation"),
    ],
)
def test_a_client_that_is_too_wide_or_unaudited_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: dict[str, tuple[int, Any]],
    session: dict[str, Any],
    match: str,
) -> None:
    keycloak = _Keycloak()
    keycloak.overrides = {key: keycloak.response(*answer) for key, answer in overrides.items()}

    with pytest.raises(E2EError, match=match):
        _run(monkeypatch, tmp_path, keycloak, **session)


def test_a_contract_without_the_admin_block_fails(tmp_path: Path) -> None:
    identity = {k: v for k, v in _IDENTITY.items() if k != "admin"}

    with pytest.raises(E2EError, match="emit shared.identity.admin"):
        _identity_admin.check_admin_client(e2e_cfg(tmp_path), identity, _BASE, None)  # type: ignore[arg-type]
