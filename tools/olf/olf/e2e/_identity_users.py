"""`olf users` against the real issuer (#331), on a throwaway user made and deleted by `olf-users`."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

import requests

from olf import k8s, log
from olf.e2e._identity_admin import REQUEST_TIMEOUT
from olf.e2e._shell import E2EConfig, E2EError
from olf.identity_admin import KeycloakAdmin, RoleModel


def users_admin(
    cfg: E2EConfig, identity: dict[str, Any], base_url: str, email_delivery: bool = False
) -> tuple[KeycloakAdmin, str]:
    """The `olf users` client the deployed contract describes, and its client secret."""
    realm, client_id = identity["issuer_url"].rsplit("/", 1)[1], identity["admin"]["client_id"]
    ref = identity["admin"]["secret_ref"]
    secret = k8s.secret_value(ref["name"], ref["key"], cfg.platform_namespace, kube_context=cfg.kube_context)
    roles = RoleModel.from_contract(identity)
    return KeycloakAdmin(base_url, realm, client_id, secret, roles, email_delivery=email_delivery), secret


def check_users_administration(
    cfg: E2EConfig, identity: dict[str, Any], base_url: str, claims: Callable[[str, str], dict[str, Any]]
) -> None:
    """`claims(username, password)` returns the ID token's claims, or raises E2EError if login is refused."""
    log.step("Checking olf users: grant, revoke, disable and enable...")
    realm, client_id = identity["issuer_url"].rsplit("/", 1)[1], identity["admin"]["client_id"]
    admin, secret = users_admin(cfg, identity, base_url)
    token = requests.post(
        f"{base_url}/realms/{realm}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
        timeout=REQUEST_TIMEOUT,
    )
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    name, password = f"olf-e2e-users-{secrets.token_hex(3)}", secrets.token_urlsafe(24)
    created = requests.post(
        f"{base_url}/admin/realms/{realm}/users",
        headers=headers,
        json={
            "username": name,
            "enabled": True,
            "emailVerified": True,
            "email": f"{name}@example.invalid",
            "firstName": "OLF",
            "lastName": "E2E",
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    created.raise_for_status()
    lowest, higher = identity["roles"]["precedence"][-1], identity["roles"]["precedence"][-2]

    def expect_roles(expected: list[str]) -> None:
        got = claims(name, password).get(identity["role_claim"], [])
        if got != expected:
            raise E2EError(f"{name}: expected {identity['role_claim']}={expected}, got {got!r}")

    try:
        admin.grant(name, higher)
        expect_roles([higher])
        admin.revoke(name, higher)
        expect_roles([])
        admin.grant(name, lowest)
        admin.set_enabled(name, False)
        if next(user.state for user in admin.users() if user.username == name) != "disabled":
            raise E2EError(f"{name} should list as disabled")
        try:
            claims(name, password)
        except E2EError:
            admin.set_enabled(name, True)
            expect_roles([lowest])
        else:
            raise E2EError(f"{name} logged in after `olf users disable`")
    finally:
        requests.delete(created.headers["Location"], headers=headers, timeout=REQUEST_TIMEOUT)
