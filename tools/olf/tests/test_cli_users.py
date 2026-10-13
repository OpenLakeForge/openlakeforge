from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from test_identity_admin import IDENTITY, FakeKeycloak, _admin
from typer.testing import CliRunner

from olf import identity_admin
from olf.cli import app
from olf.commands import users

runner = CliRunner()


def _run(*args: str) -> Any:
    return runner.invoke(app, ["users", *args])


def _issuer(monkeypatch: pytest.MonkeyPatch, identity: dict[str, Any], fake: FakeKeycloak | None = None) -> None:
    @contextlib.contextmanager
    def connect(_identity: Any, **_kwargs: Any) -> Iterator[identity_admin.KeycloakAdmin]:
        yield _admin(fake or FakeKeycloak())

    monkeypatch.setattr(users, "_identity", lambda *_args: (identity, "olf-system", "kind-x"))
    monkeypatch.setattr(users.identity_admin, "connect", connect)


def test_users_are_listed_changed_and_flagged_when_they_hold_no_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _issuer(monkeypatch, IDENTITY, FakeKeycloak())

    assert json.loads(_run("roles", "--json").output)["roles"][3] == {"role": "viewer", "groups": ["viewer"]}
    listed = json.loads(_run("list", "--json").output)
    assert listed["unrecognised"] == ["cy"]
    assert {u["username"]: u["state"] for u in listed["users"]} == {"ada": "active", "bob": "pending", "cy": "disabled"}
    assert "NO ROLE" in _run("list").output


def test_grant_revoke_disable_and_enable_change_the_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeycloak()
    _issuer(monkeypatch, IDENTITY, fake)

    for args in (("grant", "cy", "--role", "analyst"), ("revoke", "ada", "--role", "viewer"), ("disable", "bob")):
        assert _run(*args).exit_code == 0
    by_name = {u.username: u for u in _admin(fake).users()}

    assert by_name["cy"].roles == ("analyst",) and by_name["ada"].roles == ("platform-admin",)
    assert by_name["bob"].state == "disabled"
    assert _run("grant", "ada", "--role", "root").exit_code == 1


def test_without_an_admin_api_the_command_explains_and_roles_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = {k: v for k, v in IDENTITY.items() if k != "admin"} | {"capabilities": {"admin_api": False}}
    monkeypatch.setattr(users, "_identity", lambda *_args: (identity, "olf-system", "kind-x"))

    denied = _run("list")
    assert denied.exit_code == 1 and "administration surface" in denied.output
    assert _run("roles").exit_code == 0
