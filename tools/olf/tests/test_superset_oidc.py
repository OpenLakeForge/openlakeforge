from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
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
BARE = {role: [role] for role in DEX}


def _cfg(role_mapping: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "issuer_url": "https://auth.olf.localhost/",
        "client_id": "superset",
        "scope": "openid email profile groups offline_access",
        "role_claim": "groups",
        "role_mapping": role_mapping,
        "grants": GRANTS,
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


def test_the_superset_module_keeps_local_login_unless_an_issuer_is_given() -> None:
    module = ROOT / "infra/terraform/modules/analytics/superset"
    variables = (module / "variables.tf").read_text()
    oidc = variables[variables.index('variable "oidc"') :]

    assert "default = null" in oidc
    assert "var.oidc == null ? [] :" in (module / "main.tf").read_text()


def test_the_superset_client_redirects_to_the_exact_callback() -> None:
    contracts = (ROOT / "infra/terraform/environments/local/contracts.tf").read_text()

    assert '/oauth-authorized/olf"]' in contracts


def _security(monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Load olf_security with Superset and Flask stubbed out."""
    import sys
    import types

    session: dict[str, Any] = {}
    flask = types.ModuleType("flask")
    flask.session = session  # type: ignore[attr-defined]
    base = types.ModuleType("superset.security")
    fab = types.ModuleType("flask_appbuilder")
    fab.expose = lambda *_a, **_k: (lambda f: f)  # type: ignore[attr-defined]
    views = types.ModuleType("flask_appbuilder.security.views")

    class AuthOAuthView:
        def login(self, provider: str | None = None) -> str | None:
            return provider

    views.AuthOAuthView = AuthOAuthView  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flask_appbuilder", fab)
    monkeypatch.setitem(sys.modules, "flask_appbuilder.security", types.ModuleType("flask_appbuilder.security"))
    monkeypatch.setitem(sys.modules, "flask_appbuilder.security.views", views)

    class SupersetSecurityManager:
        def auth_user_oauth(self, userinfo: dict[str, Any]) -> str:
            return str(userinfo["username"])

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
    return manager, session


def test_a_user_with_no_mapped_role_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})

    assert manager.auth_user_oauth({"username": "oidc-eve", "role_keys": ["acme:other"]}) is None
    assert manager.auth_user_oauth({"username": "oidc-eve", "role_keys": []}) is None
    assert "olf_login_at" not in session


def test_a_mapped_user_signs_in_and_starts_the_session_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})

    assert manager.auth_user_oauth({"username": "oidc-ann", "role_keys": ["acme:admins"]}) == "oidc-ann"
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


def test_the_login_page_goes_straight_to_the_only_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _security(monkeypatch, {})
    import types

    cls = type(manager).authoauthview
    view = cls()
    view.invalid_login_message = "refused"
    view.appbuilder = types.SimpleNamespace(sm=types.SimpleNamespace(oauth_providers=[{"name": "olf"}]))

    assert view.login() == "olf"
    assert view.login("other") == "other"
    session["_flashes"] = [("message", "Please log in")]  # ordinary unauthorized redirect
    assert view.login() == "olf"
    session["_flashes"] = [("warning", "refused")]  # a refused user must not loop
    assert view.login() is None
