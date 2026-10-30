"""Reconcile OpenMetadata roles from GitHub team membership (ADR 0014, #25).

This file runs inside the pinned `ingestion-base` image, which has no `olf`: it is mounted from a
ConfigMap, so it imports the standard library only. `_app_jwt` is a copy of the signing in
`olf/infra/github_app.py`; the image's `cryptography` signs it, or the `openssl` CLI when that is absent.
No token or key is ever printed: errors name the HTTP status and URL path only.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

WRITE_ROLE = "DataSteward"
# Never reconciled: the local admin has no credential under SSO, the bot is how this runs.
PROTECTED = frozenset({"admin", "olf-automation"})

Json = dict[str, Any]
Http = Callable[..., Any]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(message: bytes, pem: str) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as key:  # mode 0600; removed on close
            key.write(pem)
            key.flush()
            try:
                done = subprocess.run(
                    ["openssl", "dgst", "-sha256", "-sign", key.name], input=message, capture_output=True, check=True
                )
            except (OSError, subprocess.CalledProcessError):
                raise SystemExit(
                    "cannot sign the GitHub App JWT: the image has neither the 'cryptography' module nor 'openssl'"
                ) from None
            return done.stdout
    private: Any = serialization.load_pem_private_key(pem.encode(), password=None)
    return bytes(private.sign(message, padding.PKCS1v15(), hashes.SHA256()))


def _app_jwt(app_id: str, pem: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    head = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": issued - 60, "exp": issued + 540, "iss": str(app_id)}
    signing_input = ".".join(_b64(json.dumps(part).encode()) for part in (head, claims))
    return f"{signing_input}.{_b64(_sign(signing_input.encode(), pem))}"


def _request(
    method: str, url: str, token: str, body: Any = None, content_type: str = "application/json"
) -> tuple[Any, str]:
    request = urllib.request.Request(
        url,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": content_type},
    )
    path = urllib.parse.urlsplit(url).path
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URLs come from our own config
            raw = response.read()
            return (json.loads(raw) if raw else {}), response.headers.get("Link", "")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} failed: HTTP {exc.code}") from None
    except OSError as exc:
        raise SystemExit(f"{method} {path} failed: {type(exc).__name__}") from None


def team_roles(
    api: str, token: str, org: str, teams: Mapping[str, list[str]], precedence: list[str], http: Http = _request
) -> dict[str, str]:
    """Lowercase GitHub login to its highest canonical role, over every mapped team.

    `precedence` is release/identity-roles.yaml, highest first: a member of several teams holds the highest.
    """
    held: dict[str, str] = {}
    for role in precedence:
        for slug in teams.get(role, []):
            # The members API includes child teams' people, which would grant them the parent's role. Raising
            # here, before reconcile runs, changes nothing in OpenMetadata.
            children, _ = http("GET", f"{api}/orgs/{org}/teams/{slug}/teams?per_page=1", token)
            if children:
                raise SystemExit(f"map direct teams only; {slug} has child teams")
            page = f"{api}/orgs/{org}/teams/{slug}/members?per_page=100"
            while page:
                members, link = http("GET", page, token)
                for member in members:
                    held.setdefault(member["login"].lower(), role)  # precedence order: first wins
                page = next((part.split(";")[0].strip(" <>") for part in link.split(",") if 'rel="next"' in part), "")
    return held


def installation_token(api: str, org: str, app_id: str, pem: str, http: Http = _request) -> str:
    jwt = _app_jwt(app_id, pem)
    installation, _ = http("GET", f"{api}/orgs/{org}/installation", jwt)
    minted, _ = http("POST", f"{api}/app/installations/{installation['id']}/access_tokens", jwt, {})
    return str(minted["token"])


def reconcile(om: str, token: str, held: Mapping[str, str], http: Http = _request) -> list[str]:
    """Patch each OpenMetadata user whose admin flag or write role differs; return the names changed."""
    role, _ = http("GET", f"{om}/api/v1/roles/name/{WRITE_ROLE}", token)
    ref = {"id": role["id"], "type": "role"}
    changed: list[str] = []
    after = ""
    while True:
        query = "limit=100&fields=roles" + (f"&after={urllib.parse.quote(after)}" if after else "")
        page, _ = http("GET", f"{om}/api/v1/users?{query}", token)
        for user in page["data"]:
            name = user["name"].lower()
            if user.get("isBot") or name in PROTECTED:
                continue
            canonical = held.get(name)  # in no mapped team: the baseline, whatever the user held
            want_admin = canonical == "platform-admin"
            want_write = canonical == "data-engineer"
            roles = user.get("roles") or []
            ops: list[Json] = []
            if bool(user.get("isAdmin")) != want_admin:
                ops.append({"op": "add", "path": "/isAdmin", "value": want_admin})
            if any(r.get("name") == WRITE_ROLE for r in roles) != want_write:
                kept = [{"id": r["id"], "type": "role"} for r in roles if r.get("name") != WRITE_ROLE]
                ops.append({"op": "add", "path": "/roles", "value": kept + ([ref] if want_write else [])})
            if ops:
                http("PATCH", f"{om}/api/v1/users/{user['id']}", token, ops, "application/json-patch+json")
                changed.append(name)
        after = page.get("paging", {}).get("after") or ""
        if not after:
            return changed


def main(environ: Mapping[str, str] = os.environ) -> None:
    required = (
        "GITHUB_API", "GITHUB_ORG", "ROLE_TEAMS", "ROLE_PRECEDENCE", "OM_URL", "OM_JWT", "APP_ID", "PRIVATE_KEY"
    )
    missing = [key for key in required if not environ.get(key)]
    if missing:
        raise SystemExit(
            f"missing configuration {missing}; APP_ID and PRIVATE_KEY come from the Secret 'olf github connect' "
            "writes, which a hand-made OAuth App does not have"
        )
    api, org = environ["GITHUB_API"], environ["GITHUB_ORG"]
    github = installation_token(api, org, environ["APP_ID"], environ["PRIVATE_KEY"])
    held = team_roles(api, github, org, json.loads(environ["ROLE_TEAMS"]), json.loads(environ["ROLE_PRECEDENCE"]))
    changed = reconcile(environ["OM_URL"], environ["OM_JWT"], held)
    print(f"role sync: {len(held)} GitHub members mapped, {len(changed)} OpenMetadata users changed: {changed}")


if __name__ == "__main__":
    main()
