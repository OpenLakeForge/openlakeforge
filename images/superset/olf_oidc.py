"""Superset settings for the identity.oidc contract (ADR 0014).

Pure functions, so tests import this file without Superset installed.
`olf_security.py` holds the part that needs Flask-AppBuilder.
"""

from __future__ import annotations

SESSION_SECONDS = 15 * 60
NO_ACCESS_ROLE = "olf_no_access"
VIEWER_ROLE = "olf_viewer"
TRINO_AUDIENCE = "audience:server:client_id:olf-cli"
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


def _sign_in_scope(cfg: dict) -> str:
    """The sign-in token is reused for Trino (`olf_trino.store_login_token`), so it must be one Trino and Polaris take.

    Trino requires its audience; Polaris names the person from `federated_claims`.
    Naming any audience replaces the default one in Dex, so Superset's own is
    named too, or the login's id_token fails its audience check.
    """
    if not cfg.get("people_catalog"):
        return cfg["scope"]
    return f"{cfg['scope']} federated:id {TRINO_AUDIENCE} audience:server:client_id:{cfg['client_id']}"


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
                    "client_kwargs": {"scope": _sign_in_scope(cfg)},
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


# Legacy viz.py chart types: their cache key has no user, so one user's cached
# result would be served to another. `CACHE_QUERY_BY_USER` covers the rest.
LEGACY_VIZ_TYPES = [
    "time_table", "cal_heatmap", "bubble", "bullet", "line", "time_pivot", "compare", "chord", "country_map",
    "world_map", "para", "horizon", "mapbox", "deck_multi", "deck_scatter", "deck_screengrid", "deck_grid",
    "deck_path", "deck_polygon", "deck_hex", "deck_heatmap", "deck_contour", "deck_geojson", "deck_arc",
    "event_flow", "paired_ttest", "rose", "partition",
]  # fmt: skip


def trino_settings(cfg: dict, client_secret: str) -> dict:
    """Superset's per-user Trino sign-in on the same client as the login (ADR 0019 section 9)."""
    issuer = cfg["issuer_url"].rstrip("/")
    return {
        "DATABASE_OAUTH2_CLIENTS": {
            "Trino": {
                "id": cfg["client_id"],
                "secret": client_secret,
                "scope": f"openid email groups federated:id offline_access {TRINO_AUDIENCE}",
                "authorization_request_uri": f"{issuer}/auth",
                "token_request_uri": f"{issuer}/token",
                "request_content_type": "data",  # Dex takes a form body, not JSON
            }
        },
        "DATABASE_OAUTH2_REDIRECT_URI": cfg["database_redirect_uri"],
        # Alerts, reports, thumbnails and cache warm-up run as owners that hold
        # no Dex token.
        "FEATURE_FLAGS": {
            "CACHE_QUERY_BY_USER": True,
            "CACHE_IMPERSONATION": True,
            "ALERT_REPORTS": False,
            "THUMBNAILS": False,
        },
        "VIZ_TYPE_DENYLIST": LEGACY_VIZ_TYPES,
        # Read by patches/patch_trino_iceberg_partitions.py.
        "OLF_ICEBERG_CATALOGS": ["iceberg", cfg["people_catalog"]],
    }
