from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import e2e_cfg

from olf.e2e import _identity, _identity_mail
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


class _Keycloak:
    """The admin API calls the mail checks make, answered the way Keycloak does."""

    def __init__(self, *, send_status: int = 204, closed_port_status: int = 500, realm: dict | None = None) -> None:
        self.send_statuses = [send_status, closed_port_status]
        self.realm = realm or {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_kwargs: Any) -> SimpleNamespace:
        self.calls.append((method, url))
        if url.endswith("/execute-actions-email"):
            status = self.send_statuses.pop(0)
            return SimpleNamespace(status_code=status, text="Failed to send execute actions email: boom")
        if method == "GET":
            return SimpleNamespace(json=lambda: self.realm, raise_for_status=lambda: None)
        return SimpleNamespace(headers={"Location": "https://kc/users/u1"}, raise_for_status=lambda: None)


def _sink(monkeypatch: pytest.MonkeyPatch, delivered: set[str]) -> None:
    monkeypatch.setattr(_identity_mail, "_mail_sink", lambda _cfg: contextlib.nullcontext())
    monkeypatch.setattr(_identity_mail, "_sink_recipients", lambda _cfg: delivered)
    monkeypatch.setattr(_identity_mail.time, "sleep", lambda _s: None)


def test_a_delivered_action_email_and_a_reported_failure_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _sink(monkeypatch, {"delivered@example.invalid"})
    keycloak = _Keycloak()

    _identity_mail.check_email_delivery(e2e_cfg(tmp_path), keycloak, "https://kc")

    assert keycloak.calls[-1][0] == "DELETE"  # the throwaway realm is always removed


def test_an_accepted_email_that_never_arrives_is_a_delivery_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sink(monkeypatch, set())
    monkeypatch.setattr(_identity_mail, "DELIVERY_WAIT_SECONDS", -1)
    keycloak = _Keycloak()

    with pytest.raises(E2EError, match="never reached the mail sink"):
        _identity_mail.check_email_delivery(e2e_cfg(tmp_path), keycloak, "https://kc")
    assert keycloak.calls[-1][0] == "DELETE"


def test_an_unreachable_smtp_server_must_be_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sink(monkeypatch, {"delivered@example.invalid"})

    with pytest.raises(E2EError, match="not reported as a delivery failure"):
        _identity_mail.check_email_delivery(
            e2e_cfg(tmp_path), _Keycloak(closed_port_status=204), "https://kc"
        )


@pytest.mark.parametrize(
    ("email_delivery", "realm", "match"),
    [
        (True, {"resetPasswordAllowed": False, "verifyEmail": False}, "email_delivery=True"),
        (False, {"resetPasswordAllowed": True, "verifyEmail": False}, "email_delivery=False"),
        (True, {"resetPasswordAllowed": True, "verifyEmail": True, "smtpServer": {}}, "no smtpServer host"),
    ],
)
def test_a_realm_that_offers_mail_flows_without_mail_is_reported(
    email_delivery: bool, realm: dict, match: str
) -> None:
    identity = {"capabilities": {"email_delivery": email_delivery}}

    with pytest.raises(E2EError, match=match):
        _identity_mail.check_realm_mail_matches_contract(identity, _Keycloak(realm=realm), "https://kc/realm")


def test_a_realm_without_mail_and_a_contract_without_it_agree() -> None:
    realm = {"resetPasswordAllowed": False, "verifyEmail": False, "smtpServer": {}}

    _identity_mail.check_realm_mail_matches_contract({}, _Keycloak(realm=realm), "https://kc/realm")
