from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import e2e_cfg

from olf.e2e import _identity
from olf.e2e._shell import E2EError

_IDENTITY: dict[str, Any] = {
    "implementation": "identity.oidc",
    "adapter": "keycloak",
    "issuer_url": "https://auth.olf.localhost/realms/openlakeforge",
    "clients": {"perimeter": {"client_id": "perimeter", "secret_ref": {"name": "s", "key": "client-secret"}}},
}


def test_an_issuer_that_is_not_keycloak_is_not_probed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    external = {"implementation": "identity.oidc", "adapter": "external"}
    monkeypatch.setattr(_identity, "load_provider_contracts_or_raise", lambda _cfg: {"shared": {"identity": external}})
    monkeypatch.setattr(_identity.access, "service_url", pytest.fail)

    _identity.check_identity(e2e_cfg(tmp_path))


def _token_endpoint(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> None:
    monkeypatch.setattr(_identity, "_client_secret", lambda *_args: "referenced")
    monkeypatch.setattr(
        _identity.requests,
        "post",
        lambda _url, data, **_kwargs: SimpleNamespace(json=lambda: {"error": answers[data["client_secret"]]}),
    )


def test_a_client_secret_the_issuer_accepts_for_any_value_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _token_endpoint(monkeypatch, {"referenced": "invalid_grant", "not-the-secret": "invalid_grant"})

    with pytest.raises(E2EError, match="perimeter"):
        _identity.check_client_secret_is_enforced(e2e_cfg(tmp_path), _IDENTITY, "https://x/oauth2/callback")


def test_a_client_secret_the_issuer_enforces_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _token_endpoint(monkeypatch, {"referenced": "invalid_grant", "not-the-secret": "unauthorized_client"})

    _identity.check_client_secret_is_enforced(e2e_cfg(tmp_path), _IDENTITY, "https://x/oauth2/callback")
