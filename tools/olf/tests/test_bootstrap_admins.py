from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from test_identity_admin import IDENTITY, FakeKeycloak, _admin

from olf import identity_admin
from olf.deployment import bootstrap_admins
from olf.deployment.errors import DeploymentError

ADMINS = ["ada@example.org", "new@example.org"]  # ada exists in the fake; new does not


def _state(fake: FakeKeycloak) -> dict[str, tuple[Any, ...]]:
    return {u.username: (u.state, u.roles) for u in _admin(fake).users()}


def test_only_missing_accounts_are_invited_as_platform_admin_and_existing_ones_are_untouched() -> None:
    fake = FakeKeycloak()
    before = _state(fake)

    outcome = bootstrap_admins.invite_missing(_admin(fake), [*ADMINS, "bob", "cy"])

    assert (outcome.invited, outcome.existing) == (["new@example.org"], ["ada@example.org", "bob", "cy"])
    after = _state(fake)
    assert after.pop("new@example.org") == ("pending", ("platform-admin",))
    assert after == before  # pending bob, disabled cy and active ada: no state, role or mail change
    assert len(fake.mails) == 1


def test_a_redeploy_does_not_undo_offboarding_or_resend_mail() -> None:
    fake = FakeKeycloak()
    admin = _admin(fake)
    bootstrap_admins.invite_missing(admin, ADMINS)
    admin.revoke("ada@example.org", "platform-admin")
    admin.set_enabled("ada@example.org", False)
    mails = len(fake.mails)
    offboarded = _state(fake)

    outcome = bootstrap_admins.invite_missing(admin, ADMINS)

    assert (outcome.invited, outcome.unsent, outcome.failed) == ([], {}, {})
    assert (_state(fake), len(fake.mails)) == (offboarded, mails)


def test_an_undelivered_invitation_leaves_no_account_so_the_next_deploy_retries_it() -> None:
    fake = FakeKeycloak()
    fake.smtp_down = True

    first = bootstrap_admins.invite_missing(_admin(fake), ["new@example.org"])

    assert list(first.unsent) == ["new@example.org"] and "new@example.org" not in _state(fake)
    fake.smtp_down = False
    assert bootstrap_admins.invite_missing(_admin(fake), ["new@example.org"]).invited == ["new@example.org"]


def test_without_mail_nothing_is_created_and_the_address_is_reported() -> None:
    fake = FakeKeycloak()

    outcome = bootstrap_admins.invite_missing(_admin(fake, email_delivery=False), ADMINS)

    assert (outcome.existing, list(outcome.unsent)) == (["ada@example.org"], ["new@example.org"])
    assert "no mail server" in outcome.unsent["new@example.org"] and "new@example.org" not in _state(fake)


def _connect(monkeypatch: pytest.MonkeyPatch, admin: identity_admin.KeycloakAdmin) -> None:
    @contextlib.contextmanager
    def connect(*_args: Any, **_kwargs: Any) -> Iterator[identity_admin.KeycloakAdmin]:
        yield admin

    monkeypatch.setattr(identity_admin, "connect", connect)


def test_onboard_reports_mail_problems_but_fails_the_deploy_when_the_issuer_cannot_be_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect(monkeypatch, _admin(FakeKeycloak(), email_delivery=False))
    bootstrap_admins.onboard(IDENTITY, ADMINS, namespace="olf-system", kube_context="kind-x")  # warns, no error

    _connect(monkeypatch, _admin(FakeKeycloak(), secret="wrong"))
    with pytest.raises(DeploymentError, match="olf-users client credentials"):
        bootstrap_admins.onboard(IDENTITY, ADMINS, namespace="olf-system", kube_context="kind-x")


def test_an_issuer_without_an_admin_api_is_reported_not_an_error() -> None:
    external = {k: v for k, v in IDENTITY.items() if k != "admin"} | {"capabilities": {"admin_api": False}}

    bootstrap_admins.onboard(external, ADMINS, namespace="olf-system", kube_context="kind-x")
