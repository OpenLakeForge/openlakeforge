"""The authentication half of the OpenMetadata bootstrap: admin, automation bot, ingestion bot.

Ports the first steps of the governance module's `bootstrap.sh.tftpl` (#562). Every step
is idempotent, so a Job that died half-way reruns from the top.
"""

from __future__ import annotations

import secrets
import string
import time

from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.common import log
from olf.infra import k8s
from olf.infra.openmetadata._auth import (
    ADMIN_SECRET,
    AUTOMATION_BOT,
    AUTOMATION_JWT_KEY,
    INGESTION_BOT_JWT_KEY,
    INGESTION_BOT_SECRET,
    automation_token,
)

HEALTH_ATTEMPTS = 120
HEALTH_DELAY = 5.0
_BOT = "olf-automation"
_JSON = {"JWTTokenExpiry": "Unlimited"}


def new_admin_password() -> str:
    """OpenMetadata wants 8-56 characters with each class; the suffix guarantees them."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24)) + "Aa1!"


def ensure_admin_password(namespace: str) -> str:
    """The admin password, created only if missing so Terraform never holds it (#400).

    Stored before OpenMetadata is told about it: a Job that dies in between retries
    with the Secret, never without the password.
    """
    try:
        existing = k8s.secret_value(ADMIN_SECRET, "admin-password", namespace)
    except k8s.KubectlError:
        existing = ""
    if existing:
        return existing
    password = new_admin_password()
    # create-only: if the read above failed for another reason, an existing password is never replaced
    k8s.write_secret(ADMIN_SECRET, namespace, {"admin-password": password}, overwrite=False)
    return password


def wait_healthy(base_url: str) -> None:
    probe = OpenMetadataClient(base_url)
    for attempt in range(HEALTH_ATTEMPTS):
        try:
            probe.send("GET", "/api/v1/system/config/jwks")
            return
        except ServiceClientError:
            if attempt + 1 < HEALTH_ATTEMPTS:
                time.sleep(HEALTH_DELAY)
    raise OpenMetadataError("OpenMetadata did not become healthy in time")


def _token_works(base_url: str, token: str, *, user_id: str | None = None) -> bool:
    try:
        user = OpenMetadataClient(base_url, token=token).request("GET", "/api/v1/users/loggedInUser")
    except ServiceClientError as exc:
        if exc.status not in (401, 403):
            raise
        return False
    return user_id is None or user.get("id") == user_id


def admin_client(base_url: str, email: str, password: str, stored_token: str | None) -> OpenMetadataClient:
    """A client authenticated as an administrator.

    Once SSO is on, basic login answers 403, so later runs use the automation bot token the
    first run stored. An absent or stale token (a rebuilt database) falls through to basic
    login, and a fresh server still has the built-in `admin` / `admin` account.
    """
    if stored_token and _token_works(base_url, stored_token):
        return OpenMetadataClient(base_url, token=stored_token)
    client = OpenMetadataClient(base_url)
    try:
        client.login(email, password)
        return client
    except OpenMetadataError as exc:
        login_error = exc
    try:
        client.login(email, "admin")
    except OpenMetadataError:
        raise OpenMetadataError(f"Failed to obtain admin JWT: {login_error}") from login_error
    try:
        client.request(
            "PUT",
            "/api/v1/users/changePassword",
            json_body={
                "username": "admin",
                "oldPassword": "admin",
                "newPassword": password,
                "confirmPassword": password,
                "requestType": "SELF",
            },
        )
    except OpenMetadataError as exc:
        raise OpenMetadataError(f"OpenMetadata refused the admin password change: {exc}") from exc
    client.login(email, password)
    return client


def _bearer_token(client: OpenMetadataClient, user_id: str, who: str) -> str:
    """Mint an unlimited JWT for the user and read it back; reject an encrypted (fernet) value."""
    client.request("PUT", f"/api/v1/users/generateToken/{user_id}", json_body=_JSON)
    token = client.request("GET", f"/api/v1/users/token/{user_id}").get("JWTToken") or ""
    if not token or token.startswith("fernet:"):
        raise OpenMetadataError(f"OpenMetadata did not return a bearer JWT for {who}")
    return token


def ensure_automation_bot(client: OpenMetadataClient, base_url: str, namespace: str) -> None:
    """Create the admin bot `olf` and later runs use instead of a person, and store its token."""
    user = client.request(
        "PUT",
        "/api/v1/users",
        ok_statuses=(200, 201),
        json_body={
            "name": _BOT,
            "email": "olf-automation@open-metadata.org",
            "displayName": "OpenLakeForge automation",
            "isBot": True,
            "isAdmin": True,
            "botName": _BOT,
            "authenticationMechanism": {"authType": "JWT", "config": _JSON},
        },
    )
    if not user.get("id"):
        raise OpenMetadataError(f"Failed to create the {_BOT} bot user: {user}")
    try:  # best effort: it only makes the bot visible in the UI
        client.request(
            "PUT",
            "/api/v1/bots",
            ok_statuses=(200, 201),
            json_body={"name": _BOT, "botUser": _BOT, "displayName": "OpenLakeForge automation"},
        )
    except ServiceClientError as exc:
        log.warn(f"Could not register the {_BOT} bot in the UI: {exc}")
    token = _bearer_token(client, user["id"], f"the {_BOT} bot")
    me = OpenMetadataClient(base_url, token=token).request("GET", "/api/v1/users/loggedInUser")
    if not me.get("isAdmin"):
        raise OpenMetadataError(f"The {_BOT} bot is not an administrator")
    k8s.write_secret(AUTOMATION_BOT, namespace, {AUTOMATION_JWT_KEY: token})


def ensure_ingestion_bot_token(client: OpenMetadataClient, namespace: str) -> None:
    """Keep the ingestion bot's working credential, or mint one when it is missing or stale."""
    user = client.request("GET", "/api/v1/users/name/ingestion-bot")
    if not user.get("id"):
        raise OpenMetadataError(f"Failed to find ingestion-bot user: {user}")
    stored = k8s.secret_value(INGESTION_BOT_SECRET, INGESTION_BOT_JWT_KEY, namespace, missing_ok=True)
    # Stage Secrets are replicated by a one-shot Terraform Job. Rotating a
    # working token here would revoke every stage's unchanged copy.
    if stored and _token_works(client.base_url, stored, user_id=user["id"]):
        return
    token = _bearer_token(client, user["id"], "ingestion-bot")
    k8s.write_secret(INGESTION_BOT_SECRET, namespace, {INGESTION_BOT_JWT_KEY: token})


def bootstrap_auth(base_url: str, namespace: str, *, email: str, sso: bool) -> None:
    """Admin login, the automation bot (SSO only) and the ingestion bot token."""
    password = ensure_admin_password(namespace)
    wait_healthy(base_url)
    stored = automation_token(namespace)
    client = admin_client(base_url, email, password, stored)
    # Created while basic login still works; with SSO off basic login keeps working, so no bot.
    if sso and client.token != stored:
        ensure_automation_bot(client, base_url, namespace)
    ensure_ingestion_bot_token(client, namespace)
