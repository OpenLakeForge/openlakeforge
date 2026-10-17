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
# Dex: `<organisation>:<team-slug>`. Keycloak: the group named as the role.
DEX = {
    "platform-admin": ["acme:admins"],
    "data-engineer": ["acme:engineers"],
    "analyst": ["acme:analysts"],
    "viewer": ["acme:viewers"],
}
KEYCLOAK = {role: [role] for role in DEX}


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
    mapping = olf_oidc.settings(_cfg(KEYCLOAK), "s")["AUTH_ROLES_MAPPING"]

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
