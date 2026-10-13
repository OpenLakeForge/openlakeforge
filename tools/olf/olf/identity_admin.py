"""Administer users in the issuer through `shared.identity.admin` (ADR 0014 seam 4, #331).
Nothing here returns or prints a secret, a token or a setup link."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import requests

from olf import k8s

TIMEOUT = 15
PAGE = 200
KEYCLOAK_DRIVER = "identity.admin.keycloak"
NO_ADMIN = (
    "This issuer exposes no admin API (shared.identity.capabilities.admin_api is not true), so olf cannot change "
    "users. Use the issuer's own administration surface; listing users also needs a directory-read capability, "
    "which OIDC alone does not provide."
)


class AdminError(RuntimeError):
    """A failure the operator can act on; its message is safe to print."""


@dataclass(frozen=True)
class RoleModel:
    """Canonical roles, highest first, and the claim values (groups) each maps to."""
    precedence: tuple[str, ...]
    groups: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_contract(cls, identity: Mapping[str, Any]) -> RoleModel:
        mapping = {role: tuple(values) for role, values in identity["role_mapping"].items()}
        return cls(tuple(identity["roles"]["precedence"]), mapping)


@dataclass(frozen=True)
class User:
    username: str
    email: str | None
    enabled: bool
    pending: bool
    roles: tuple[str, ...]  # canonical, highest first

    @property
    def state(self) -> str:
        return "disabled" if not self.enabled else "pending" if self.pending else "active"

    def as_json(self) -> dict[str, Any]:
        return {"username": self.username, "email": self.email, "state": self.state, "roles": list(self.roles)}


class KeycloakAdmin:
    """The Keycloak admin REST API as the `olf-users` client (client credentials).
    One token per instance, never renewed: it lives 5 minutes and a command takes seconds."""

    def __init__(
        self,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        roles: RoleModel,
        session: requests.Session | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._realm = realm
        self._credentials = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
        self._roles = roles
        self._http = session or requests.Session()
        self._token: str | None = None

    def _login(self) -> str:
        reply = self._http.post(
            f"{self._base}/realms/{self._realm}/protocol/openid-connect/token", data=self._credentials, timeout=TIMEOUT
        )
        if reply.status_code != 200:
            raise AdminError(
                f"The issuer refused the olf-users client credentials (HTTP {reply.status_code}). "
                "Check that Secret and the realm Job; see docs/setup/identity-secret-rotation.md."
            )
        return str(reply.json()["access_token"])

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        self._token = self._token or self._login()
        reply = self._http.request(
            method,
            f"{self._base}/admin/realms/{self._realm}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=TIMEOUT,
            **kwargs,
        )
        if reply.status_code >= 400:
            raise AdminError(f"The issuer answered {method} {path} with HTTP {reply.status_code}.")
        return reply.json() if reply.content else None

    def _pages(self, path: str) -> Iterator[dict[str, Any]]:
        first = 0
        while page := self._call("GET", path, params={"first": first, "max": PAGE}):
            yield from page
            first += PAGE

    def _group_id(self, role: str) -> str:
        names = self._roles.groups.get(role)
        if not names:
            raise AdminError(f"{role!r} is not a canonical role; choose one of: {', '.join(self._roles.precedence)}.")
        for group in self._call("GET", "/groups", params={"search": names[0], "exact": "true"}):
            if group["name"] == names[0]:
                return str(group["id"])
        raise AdminError(f"The issuer has no group {names[0]!r} for role {role!r}; re-run the platform phase.")

    def _find(self, who: str) -> dict[str, Any]:
        for field in ("username", "email"):
            found = self._call("GET", "/users", params={field: who, "exact": "true"})
            # ponytail: the user representation does not say which users are service accounts, their
            # reserved username prefix does; a person given that prefix by hand is refused too.
            if found and found[0]["username"].startswith("service-account-"):
                raise AdminError(f"{who!r} is a service account, not a person; olf users does not administer it.")
            if found:
                return dict(found[0])
        raise AdminError(f"No user {who!r} (searched by username and email).")

    def users(self) -> list[User]:
        """Every user with the canonical roles they hold, by group membership."""
        members: dict[str, set[str]] = {}
        for role in self._roles.precedence:
            for member in self._pages(f"/groups/{self._group_id(role)}/members"):
                members.setdefault(member["id"], set()).update(self._roles.groups[role])
        return [
            User(
                user["username"],
                user.get("email"),
                bool(user.get("enabled")),
                bool(user.get("requiredActions")),
                tuple(r for r in self._roles.precedence if members.get(user["id"], set()) & set(self._roles.groups[r])),
            )
            for user in self._pages("/users")
        ]

    def grant(self, who: str, role: str) -> None:
        self._call("PUT", f"/users/{self._find(who)['id']}/groups/{self._group_id(role)}")

    def revoke(self, who: str, role: str) -> None:
        self._call("DELETE", f"/users/{self._find(who)['id']}/groups/{self._group_id(role)}")

    def set_enabled(self, who: str, enabled: bool) -> None:
        user = self._find(who)
        self._call("PUT", f"/users/{user['id']}", json={**user, "enabled": enabled})
        if not enabled:
            # Ends the issuer sessions, so no refresh extends the access token already issued.
            self._call("POST", f"/users/{user['id']}/logout")


@contextlib.contextmanager
def connect(identity: Mapping[str, Any], *, secret_namespace: str, kube_context: str | None) -> Iterator[KeycloakAdmin]:
    """An admin client for a contract's `identity` binding; a `service` is reached by `kubectl port-forward`,
    never through the perimeter (docs/architecture/identity-callers.md)."""
    admin = identity.get("admin")
    if not identity.get("capabilities", {}).get("admin_api") or admin is None:
        raise AdminError(NO_ADMIN)
    if admin["implementation"] != KEYCLOAK_DRIVER:
        raise AdminError(f"No admin driver for {admin['implementation']!r}; use the issuer's own admin surface")
    service, ref = admin.get("service"), admin["secret_ref"]
    try:
        namespace = service["namespace"] if service else secret_namespace
        secret = k8s.secret_value(ref["name"], ref["key"], namespace, kube_context=kube_context)
        realm = str(identity["issuer_url"].rsplit("/", 1)[1])
        args = (realm, admin["client_id"], secret, RoleModel.from_contract(identity))
        if service is None:
            yield KeycloakAdmin(admin["admin_url"], *args)
            return
        name, port_, ns = service["name"], service["port"], service["namespace"]
        with k8s.port_forward(name, port_, ns, kube_context=kube_context) as port:
            yield KeycloakAdmin(f"http://127.0.0.1:{port}", *args)
    except k8s.KubectlError as exc:
        raise AdminError(f"Could not reach the issuer through kubectl: {exc}") from exc
