from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from olf import identity_admin
from olf.identity_admin import AdminError, KeycloakAdmin, RoleModel

ROLES = ("platform-admin", "data-engineer", "analyst", "viewer")
IDENTITY: dict[str, Any] = {
    "issuer_url": "https://auth.olf.localhost/realms/openlakeforge",
    "role_claim": "groups",
    "roles": {"precedence": list(ROLES)},
    "role_mapping": {role: [role] for role in ROLES},
    "capabilities": {"admin_api": True},
    "admin": {
        "implementation": "identity.admin.keycloak",
        "service": {"namespace": "olf-system", "name": "keycloak", "port": 8080},
        "client_id": "olf-users",
        "secret_ref": {"name": "keycloak-client-olf-users", "key": "client-secret"},
    },
}


class FakeKeycloak:
    """The slice of the Keycloak admin API that `KeycloakAdmin` uses, held in memory."""

    def __init__(self, extra_users: int = 0) -> None:
        self.users = [
            {"id": "u1", "username": "ada", "email": "ada@example.org", "enabled": True},
            {"id": "u2", "username": "bob", "email": None, "enabled": True, "requiredActions": ["UPDATE_PASSWORD"]},
            {"id": "u3", "username": "cy", "email": None, "enabled": False},
        ] + [{"id": f"x{n}", "username": f"extra{n}", "enabled": True} for n in range(extra_users)]
        self.groups = {name: f"g-{name}" for name in ROLES} | {"other": "g-other"}
        self.members: dict[str, set[str]] = {gid: set() for gid in self.groups.values()}
        self.members["g-platform-admin"].add("u1")
        self.members["g-viewer"].update({"u1", "u2"})
        self.members["g-other"].add("u3")
        self.logged_out: list[str] = []

    def post(self, url: str, data: dict[str, str], **_kwargs: Any) -> SimpleNamespace:
        if data["client_secret"] != "the-secret":
            return self._reply(401)
        return self._reply(200, {"access_token": "t"})

    def request(self, method: str, url: str, headers: dict[str, str], params: dict | None = None, **kwargs: Any):
        path, params = url.split("/admin/realms/openlakeforge", 1)[1], params or {}
        if path == "/users" and method == "GET":
            if not {"username", "email"} & set(params):
                return self._page(self.users, params)
            field, value = next((k, v) for k, v in params.items() if k in ("username", "email"))
            return self._reply(200, [u for u in self.users if u.get(field) == value])
        if path == "/groups":
            return self._reply(200, [{"id": gid, "name": n} for n, gid in self.groups.items() if params["search"] in n])
        if match := re.fullmatch(r"/groups/(.+)/members", path):
            return self._page([u for u in self.users if u["id"] in self.members[match[1]]], params)
        if match := re.fullmatch(r"/users/(.+)/groups/(.+)", path):
            (self.members[match[2]].add if method == "PUT" else self.members[match[2]].discard)(match[1])
            return self._reply(204)
        if match := re.fullmatch(r"/users/(.+)/logout", path):
            self.logged_out.append(match[1])
            return self._reply(204)
        if method == "PUT" and re.fullmatch(r"/users/[^/]+", path):
            next(u for u in self.users if u["id"] == path.rsplit("/", 1)[1]).update(kwargs["json"])
            return self._reply(204)
        return self._reply(404)

    def _page(self, rows: list[dict], params: dict) -> SimpleNamespace:
        return self._reply(200, rows[params["first"] : params["first"] + params["max"]])

    @staticmethod
    def _reply(status: int, body: Any = None) -> SimpleNamespace:
        return SimpleNamespace(status_code=status, json=lambda: body, content=b"x" if body is not None else b"")


def _admin(fake: FakeKeycloak, secret: str = "the-secret") -> KeycloakAdmin:
    return KeycloakAdmin(
        "http://127.0.0.1:1", "openlakeforge", "olf-users", secret, RoleModel.from_contract(IDENTITY), session=fake  # type: ignore[arg-type]
    )


def _state(admin: KeycloakAdmin, name: str) -> Any:
    return next(u for u in admin.users() if u.username == name)


def test_list_reports_role_state_and_users_without_a_canonical_role() -> None:
    admin = _admin(FakeKeycloak())

    assert _state(admin, "ada").roles == ("platform-admin", "viewer")  # highest first
    assert _state(admin, "bob").state == "pending"
    assert (_state(admin, "cy").roles, _state(admin, "cy").state) == ((), "disabled")  # in a non-canonical group only


def test_list_pages_through_more_users_than_one_page() -> None:
    assert len(_admin(FakeKeycloak(extra_users=identity_admin.PAGE)).users()) == identity_admin.PAGE + 3


def test_grant_adds_a_role_and_revoke_removes_one() -> None:
    admin = _admin(FakeKeycloak())

    admin.grant("bob", "data-engineer")
    assert _state(admin, "bob").roles == ("data-engineer", "viewer")
    admin.revoke("bob", "viewer")
    assert _state(admin, "bob").roles == ("data-engineer",)


def test_disable_and_enable_toggle_the_account_and_only_disable_ends_its_sessions() -> None:
    fake = FakeKeycloak()
    admin = _admin(fake)

    admin.set_enabled("ada", False)
    assert (_state(admin, "ada").state, fake.logged_out) == ("disabled", ["u1"])
    admin.set_enabled("ada", True)
    assert (_state(admin, "ada").state, fake.logged_out) == ("active", ["u1"])


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda a: a.grant("nobody", "viewer"), "No user 'nobody'"),
        (lambda a: a.grant("ada", "root"), "not a canonical role"),
        (lambda a: a.grant("service-account-olf-users", "viewer"), "service account"),
    ],
)
def test_an_unknown_user_or_role_or_a_service_account_is_an_error(call, match: str) -> None:
    fake = FakeKeycloak()
    fake.users.append({"id": "sa", "username": "service-account-olf-users", "enabled": True})

    with pytest.raises(AdminError, match=match):
        call(_admin(fake))


def test_a_rejected_client_secret_does_not_echo_it() -> None:
    with pytest.raises(AdminError, match="olf-users client credentials") as raised:
        _admin(FakeKeycloak(), secret="wrong-secret-value").users()
    assert "wrong-secret-value" not in str(raised.value)
