from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

TOKEN_ENV = "OPENLAKEFORGE_POLARIS_TOKEN"


def mint_token(environ: Mapping[str, str] | None = None) -> str:
    """One Polaris `client_credentials` token for the stage's processing principal, or "" without a REST catalog.

    The Secret is mounted whole into the pod, so the credential arrives under the key names the
    contract carries (the same lookup `floe_profile` renders). The token is frozen for the run it
    is minted for, so callers mint per run, not per process.
    """
    env = os.environ if environ is None else environ
    token_uri = env.get("OPENLAKEFORGE_CATALOG_TOKEN_URI", "")
    if not token_uri:
        return ""
    id_key = env.get("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY") or "POLARIS_FLOE_CLIENT_ID"
    secret_key = env.get("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY") or "POLARIS_FLOE_CLIENT_SECRET"
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": env[id_key],
            "client_secret": env[secret_key],
            "scope": env.get("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE") or "PRINCIPAL_ROLE:ALL",
        }
    ).encode()
    with urllib.request.urlopen(urllib.request.Request(token_uri, data=body), timeout=30) as response:
        return json.load(response)["access_token"]


@contextmanager
def polaris_token_env() -> Iterator[str]:
    """Export a freshly minted token as `OPENLAKEFORGE_POLARIS_TOKEN` for the dbt profile, then restore."""
    previous = os.environ.get(TOKEN_ENV)
    token = os.environ[TOKEN_ENV] = mint_token()
    try:
        yield token
    finally:
        if previous is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous
