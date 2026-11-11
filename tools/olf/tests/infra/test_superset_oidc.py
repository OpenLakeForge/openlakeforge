from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("olf_oidc", ROOT / "images/superset/olf_oidc.py")
assert _spec and _spec.loader
olf_oidc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(olf_oidc)

GRANTS = yaml.safe_load((ROOT / "release/identity-roles.yaml").read_text())["grants"]["reporting"]
# Dex: `<organisation>:<team-slug>`. A bare issuer: the group named as the role.
DEX = {
    "platform-admin": ["acme:admins"],
    "data-engineer": ["acme:engineers"],
    "analyst": ["acme:analysts"],
    "viewer": ["acme:viewers"],
}
SIGN_OUT = "https://oauth2.olf.localhost/oauth2/sign_out?rd=https%3A%2F%2Folf.localhost%2Fsigned-out.html"
BARE = {role: [role] for role in DEX}


def _cfg(role_mapping: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "issuer_url": "https://auth.olf.localhost/",
        "client_id": "superset",
        "scope": "openid email profile groups offline_access",
        "role_claim": "groups",
        "role_mapping": role_mapping,
        "grants": GRANTS,
        "sign_out_url": SIGN_OUT,
    }


def test_each_canonical_role_maps_to_its_native_roles() -> None:
    mapping = olf_oidc.settings(_cfg(DEX), "s")["AUTH_ROLES_MAPPING"]

    assert mapping == {
        "acme:admins": ["Admin"],
        "acme:engineers": ["Alpha"],
        "acme:analysts": ["Gamma", "olf_data_access"],
        "acme:viewers": ["olf_data_access", "olf_viewer"],
    }


def test_the_mapping_follows_the_contract_claim_values_not_the_issuer() -> None:
    mapping = olf_oidc.settings(_cfg(BARE), "s")["AUTH_ROLES_MAPPING"]

    assert mapping["platform-admin"] == ["Admin"]
    assert mapping["viewer"] == ["olf_data_access", "olf_viewer"]


def test_a_claim_value_shared_by_two_roles_gets_the_union() -> None:
    mapping = olf_oidc.roles_mapping({"analyst": ["g"], "data-engineer": ["g"]}, GRANTS)

    assert mapping == {"g": ["Alpha", "Gamma", "olf_data_access"]}


def test_a_role_without_a_reporting_grant_maps_to_nothing() -> None:
    assert olf_oidc.roles_mapping({"intruder": ["x"]}, GRANTS) == {}


def test_an_unknown_grant_fails_loudly() -> None:
    with pytest.raises(KeyError):
        olf_oidc.roles_mapping({"viewer": ["g"]}, {"viewer": "Superuser"})


def test_login_goes_through_the_issuer_with_least_privilege_and_a_bounded_session() -> None:
    cfg = olf_oidc.settings(_cfg(DEX), "the-secret")
    remote = cfg["OAUTH_PROVIDERS"][0]["remote_app"]

    assert remote["server_metadata_url"] == "https://auth.olf.localhost/.well-known/openid-configuration"
    assert remote["client_kwargs"]["scope"] == "openid email profile groups offline_access"
    assert remote["client_secret"] == "the-secret"
    assert cfg["AUTH_USER_REGISTRATION_ROLE"] == "olf_no_access"
    assert cfg["AUTH_ROLES_SYNC_AT_LOGIN"] is True
    assert cfg["PERMANENT_SESSION_LIFETIME"] == 15 * 60
    assert cfg["SESSION_REFRESH_EACH_REQUEST"] is False
    assert cfg["OLF_SIGN_OUT_URL"] == SIGN_OUT


def test_the_superset_module_keeps_local_login_unless_an_issuer_is_given() -> None:
    module = ROOT / "infra/terraform/modules/analytics/superset"
    variables = (module / "variables.tf").read_text()
    oidc = variables[variables.index('variable "oidc"') :]

    assert "default = null" in oidc
    assert "var.oidc == null ? [] :" in (module / "main.tf").read_text()


def test_the_superset_client_redirects_to_the_exact_callback() -> None:
    contracts = (ROOT / "infra/terraform/environments/local/contracts.tf").read_text()

    assert '/oauth-authorized/olf",' in contracts
    assert '/api/v1/database/oauth2/",' in contracts


class _Globals:
    """Flask's `g`: attributes set during one request, with `pop`."""

    def pop(self, name: str, default: Any = None) -> Any:
        return self.__dict__.pop(name, default)


def _security(monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Load olf_security with Superset and Flask stubbed out."""
    import sys
    import types

    session: dict[str, Any] = {}
    flask = types.ModuleType("flask")
    flask.session = session  # type: ignore[attr-defined]
    flask.g = _Globals()  # type: ignore[attr-defined]
    flask.redirect = lambda url: ("redirect", url)  # type: ignore[attr-defined]
    flask.current_app = types.SimpleNamespace(config={"OLF_SIGN_OUT_URL": SIGN_OUT})  # type: ignore[attr-defined]
    base = types.ModuleType("superset.security")
    fab = types.ModuleType("flask_appbuilder")
    fab.expose = lambda *_a, **_k: (lambda f: f)  # type: ignore[attr-defined]
    views = types.ModuleType("flask_appbuilder.security.views")

    class AuthOAuthView:
        def login(self, provider: str | None = None) -> str | None:
            return provider

        def logout(self) -> str:
            session["olf_login_at"] = None  # FAB's logout_user ends the session
            return "index"

    views.AuthOAuthView = AuthOAuthView  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flask_appbuilder", fab)
    monkeypatch.setitem(sys.modules, "flask_appbuilder.security", types.ModuleType("flask_appbuilder.security"))
    monkeypatch.setitem(sys.modules, "flask_appbuilder.security.views", views)

    stored: list[tuple[int, dict[str, Any]]] = []
    trino = types.ModuleType("olf_trino")
    trino.store_login_token = lambda user_id, token: stored.append((user_id, token))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "olf_trino", trino)

    class SupersetSecurityManager:
        def set_oauth_session(self, provider: str, response: dict[str, Any]) -> None:
            session["oauth"] = (response["access_token"], "")

        def auth_user_oauth(self, userinfo: dict[str, Any]) -> Any:
            return types.SimpleNamespace(id=7) if userinfo["username"] == "oidc-ann" else userinfo["username"]

    base.SupersetSecurityManager = SupersetSecurityManager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flask", flask)
    monkeypatch.setitem(sys.modules, "superset", types.ModuleType("superset"))
    monkeypatch.setitem(sys.modules, "superset.security", base)
    monkeypatch.setitem(sys.modules, "olf_oidc", olf_oidc)
    spec = importlib.util.spec_from_file_location("olf_security", ROOT / "images/superset/olf_security.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    manager = module.OlfSecurityManager()
    config = {"OLF_ROLE_CLAIM": "groups", "AUTH_ROLES_MAPPING": {"acme:admins": ["Admin"]}}
    manager.appbuilder = types.SimpleNamespace(app=types.SimpleNamespace(config=config))
    manager.oauth_remotes = {"olf": types.SimpleNamespace(userinfo=lambda: claims)}
    manager.stored = stored  # type: ignore[attr-defined]
    return manager, session


def test_a_user_with_no_mapped_role_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})

    assert manager.auth_user_oauth({"username": "oidc-eve", "role_keys": ["acme:other"]}) is None
    assert manager.auth_user_oauth({"username": "oidc-eve", "role_keys": []}) is None
    assert "olf_login_at" not in session


def test_a_mapped_user_signs_in_and_starts_the_session_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})

    assert manager.auth_user_oauth({"username": "oidc-ann", "role_keys": ["acme:admins"]}).id == 7
    assert isinstance(session["olf_login_at"], float)


def test_user_info_reads_the_contract_claim_and_prefixes_the_username(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _ = _security(monkeypatch, {"preferred_username": "admin", "email": "a@x", "groups": "acme:admins"})

    info = manager.get_oauth_user_info("olf")

    assert info["username"] == "oidc-admin"
    assert info["role_keys"] == ["acme:admins"]


def test_superset_signs_visitors_in_as_admin_only_without_an_issuer() -> None:
    main = (ROOT / "infra/terraform/modules/analytics/superset/main.tf").read_text()

    assert 'var.oidc != null || !var.public_admin ? [] :' in main
    assert 'login_user(user)' in main


def test_logout_ends_the_perimeter_session_too(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})

    assert type(manager).authoauthview().logout() == ("redirect", SIGN_OUT)
    assert session["olf_login_at"] is None  # FAB's own logout still ran first


def test_logout_lands_on_a_signed_out_page_outside_the_perimeter() -> None:
    contracts = (ROOT / "infra/terraform/environments/local/contracts.tf").read_text()
    route = contracts[contracts.index('"portal/signed-out"') :].split("}", 1)[0]
    protected = contracts[contracts.index("perimeter_protected_refs =") :].split("\n", 1)[0]

    assert 'group        = ""' in route and '"/signed-out.html"' in route
    assert "signed-out" not in protected  # built from access_routes, which lacks it


def test_the_login_page_goes_straight_to_the_only_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})
    import types

    cls = type(manager).authoauthview
    view = cls()
    view.appbuilder = types.SimpleNamespace(sm=types.SimpleNamespace(oauth_providers=[{"name": "olf"}]))

    assert view.login() == "olf"
    assert view.login("other") == "other"
    session["_flashes"] = [("message", "Please log in")]  # ordinary unauthorized redirect
    assert view.login() == "olf"
    session["_flashes"] = [("warning", "refused")]  # a refused user must not loop
    assert view.login() is None


def test_the_sign_in_token_seeds_the_trino_row_and_leaves_the_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})
    response = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    manager.set_oauth_session("olf", response)
    assert session["oauth"] == ("at", "")  # FAB needs it for userinfo()
    manager.auth_user_oauth({"username": "oidc-ann", "role_keys": ["acme:admins"]})

    assert "oauth" not in session
    assert manager.stored == [(7, response)]


def test_the_sign_in_token_names_trino_only_with_a_people_catalog() -> None:
    plain = {"scope": "openid offline_access", "client_id": "superset"}
    assert olf_oidc._sign_in_scope(plain) == "openid offline_access"

    scopes = olf_oidc._sign_in_scope({**plain, "people_catalog": "dev"}).split()
    assert {"federated:id", "audience:server:client_id:olf-cli", "audience:server:client_id:superset"} <= set(scopes)


def test_superset_metadata_uri_carries_no_password() -> None:
    # Superset 6.1.0 prints its metadata URI with str() on a SQLAlchemy 1.4 URL when the first
    # query fails, and that keeps the password (#1048). The chart takes SQLALCHEMY_DATABASE_URI
    # verbatim, so the password must reach libpq through PGPASSWORD and never a string.
    main = (ROOT / "infra/terraform/modules/analytics/superset/main.tf").read_text()
    uri = re.search(r'name\s*=\s*"SQLALCHEMY_DATABASE_URI"\s*value\s*=\s*"([^"]+)"', main)
    assert uri, "the module must set SQLALCHEMY_DATABASE_URI so the chart does not build one with DB_PASS"

    authority = uri.group(1).split("://", 1)[1].split("@", 1)[0]
    assert ":" not in authority
    assert "pass" not in uri.group(1).lower()
    assert 'name = "PGPASSWORD"' in main
    assert re.search(r'name\s*=\s*"DB_PASS"', main) is None
