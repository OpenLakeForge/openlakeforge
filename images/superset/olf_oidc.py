"""Superset settings for the identity.oidc contract (ADR 0014).

Pure functions, so tests import this file without Superset installed.
`olf_security.py` holds the part that needs Flask-AppBuilder.
"""

from __future__ import annotations

SESSION_SECONDS = 15 * 60
NO_ACCESS_ROLE = "olf_no_access"
VIEWER_ROLE = "olf_viewer"
DATA_ROLE = "olf_data_access"

# The native roles behind each `reporting` grant in release/identity-roles.yaml.
# Analysts and viewers also hold the data scope role, which carries only
# schema_access grants.
NATIVE_ROLES = {
    "Admin": ["Admin"],
    "Alpha": ["Alpha"],
    "Gamma": ["Gamma", DATA_ROLE],
    "read-only": [VIEWER_ROLE, DATA_ROLE],
}


def roles_mapping(role_mapping: dict[str, list[str]], grants: dict[str, str]) -> dict[str, list[str]]:
    """Claim value -> native roles; a value shared by several roles gets the union."""
    mapping: dict[str, list[str]] = {}
    for role, claim_values in role_mapping.items():
        if role not in grants:
            continue
        for value in claim_values:
            mapping[value] = sorted({*mapping.get(value, []), *NATIVE_ROLES[grants[role]]})
    return mapping


def settings(cfg: dict, client_secret: str) -> dict:
    issuer = cfg["issuer_url"].rstrip("/")
    return {
        "AUTH_TYPE": 4,  # flask_appbuilder.const.AUTH_OAUTH
        "OAUTH_PROVIDERS": [
            {
                "name": "olf",
                "icon": "fa-key",
                "token_key": "access_token",
                "remote_app": {
                    "client_id": cfg["client_id"],
                    "client_secret": client_secret,
                    "server_metadata_url": f"{issuer}/.well-known/openid-configuration",
                    "api_base_url": issuer,
                    "client_kwargs": {"scope": cfg["scope"]},
                },
            }
        ],
        "OLF_ROLE_CLAIM": cfg["role_claim"],
        "OLF_SIGN_OUT_URL": cfg["sign_out_url"],
        "AUTH_ROLES_MAPPING": roles_mapping(cfg["role_mapping"], cfg["grants"]),
        "AUTH_ROLES_SYNC_AT_LOGIN": True,
        "AUTH_USER_REGISTRATION": True,
        "AUTH_USER_REGISTRATION_ROLE": NO_ACCESS_ROLE,
        "PERMANENT_SESSION_LIFETIME": SESSION_SECONDS,
        "SESSION_REFRESH_EACH_REQUEST": False,
    }
