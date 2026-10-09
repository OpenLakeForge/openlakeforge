"""Email checks for the identity provider (#24, feeding #331).

Delivery runs in a throwaway realm, not the deployed one: replacing a realm's
`smtpServer` through the admin API drops its stored password (the read-back is
masked, so it cannot be restored), which would break a real relay the operator
configured. The throwaway realm has Keycloak's own mail path and no users but
the test user, and is deleted afterwards.

The mail server is a Mailpit that lives for this check only. The product stack
has no mail server and the default profile needs no SMTP. Message bodies and
links are never read or logged: only the recipient address, which the check
chose, is looked at.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import time
from collections.abc import Iterator
from typing import Any

import requests
import yaml

from olf import log
from olf.e2e._shell import E2EConfig, E2EError, kubectl

SINK = "olf-e2e-mailsink"
SMTP_PORT = 1025
# Mailpit's HTTP API port; reached through the API server's pod proxy, so the
# check needs neither a port-forward nor an image with a shell.
API_PORT = 8025
REQUEST_TIMEOUT = 15
DELIVERY_WAIT_SECONDS = 30


def _call(admin: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    return admin.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)


def check_realm_mail_matches_contract(identity: dict[str, Any], admin: requests.Session, realm_admin: str) -> None:
    """The deployed realm offers password reset and email verification exactly
    when the contract says the issuer can send mail: a reset link that cannot
    be mailed is a dead end."""
    log.step("Checking the realm's mail settings match the identity contract...")
    realm = _call(admin, "GET", realm_admin)
    realm.raise_for_status()
    realm = realm.json()
    enabled = identity.get("capabilities", {}).get("email_delivery", False)
    if (realm["resetPasswordAllowed"], realm["verifyEmail"]) != (enabled, enabled):
        raise E2EError(
            f"the contract says email_delivery={enabled}, but the realm has "
            f"resetPasswordAllowed={realm['resetPasswordAllowed']} and verifyEmail={realm['verifyEmail']}"
        )
    if enabled and not realm.get("smtpServer", {}).get("host"):
        raise E2EError("the contract says email_delivery=true, but the realm has no smtpServer host")


def _remove_sink(cfg: E2EConfig) -> None:
    kubectl(cfg, ["delete", "pod,service", SINK, "-n", cfg.platform_namespace, "--ignore-not-found", "--wait=false"])


@contextlib.contextmanager
def _mail_sink(cfg: E2EConfig) -> Iterator[None]:
    catalog = yaml.safe_load((cfg.distribution_root / "release/component-catalog.yaml").read_text())
    image = catalog["components"]["images"]["e2e_mail_sink"]
    namespace = cfg.platform_namespace
    _remove_sink(cfg)
    try:
        kubectl(cfg, ["run", SINK, "-n", namespace, "--restart=Never", f"--image={image}", f"--port={SMTP_PORT}"])
        kubectl(cfg, ["expose", "pod", SINK, "-n", namespace, f"--port={SMTP_PORT}"])
        kubectl(cfg, ["wait", "--for=condition=Ready", f"pod/{SINK}", "-n", namespace, "--timeout=180s"])
        yield
    finally:
        _remove_sink(cfg)


def _sink_recipients(cfg: E2EConfig) -> set[str]:
    proxy = f"/api/v1/namespaces/{cfg.platform_namespace}/pods/{SINK}:{API_PORT}/proxy/api/v1/messages"
    messages = json.loads(kubectl(cfg, ["get", "--raw", proxy], capture=True))["messages"]
    return {to["Address"] for message in messages for to in message["To"]}


def _wait_for_delivery(cfg: E2EConfig, recipient: str) -> None:
    deadline = time.monotonic() + DELIVERY_WAIT_SECONDS
    while recipient not in _sink_recipients(cfg):
        if time.monotonic() > deadline:
            raise E2EError(f"Keycloak accepted the action email for {recipient}, but it never reached the mail sink")
        time.sleep(1)


def _create_user(admin: requests.Session, realm_admin: str, name: str) -> tuple[str, str]:
    email = f"{name}@example.invalid"
    created = _call(
        admin, "POST", f"{realm_admin}/users", json={"username": name, "enabled": True, "email": email}
    )
    created.raise_for_status()
    return created.headers["Location"].rsplit("/", 1)[1], email


def _send_action_email(admin: requests.Session, realm_admin: str, user_id: str) -> requests.Response:
    return _call(
        admin,
        "PUT",
        f"{realm_admin}/users/{user_id}/execute-actions-email",
        params={"lifespan": 300},
        json=["UPDATE_PASSWORD"],
    )


def check_email_delivery(cfg: E2EConfig, admin: requests.Session, base_url: str) -> None:
    """A Keycloak action email arrives at an SMTP server, and an unreachable
    server is reported as a delivery failure rather than as success."""
    log.step("Checking a Keycloak action email reaches a test mail sink...")
    realm = f"olf-e2e-mail-{secrets.token_hex(3)}"
    realm_admin = f"{base_url}/admin/realms/{realm}"
    smtp = {
        "host": f"{SINK}.{cfg.platform_namespace}.svc",
        "port": str(SMTP_PORT),
        "from": "olf-e2e@example.invalid",
        "auth": "false",
        "ssl": "false",
        "starttls": "false",
    }
    with _mail_sink(cfg):
        created = _call(
            admin, "POST", f"{base_url}/admin/realms", json={"realm": realm, "enabled": True, "smtpServer": smtp}
        )
        created.raise_for_status()
        try:
            user_id, email = _create_user(admin, realm_admin, "delivered")
            sent = _send_action_email(admin, realm_admin, user_id)
            if sent.status_code != 204:
                raise E2EError(f"Keycloak did not send the action email: HTTP {sent.status_code}")
            _wait_for_delivery(cfg, email)

            # Port 1 on Keycloak's own loopback refuses the connection at once.
            _call(admin, "PUT", realm_admin, json={"smtpServer": smtp | {"host": "localhost", "port": "1"}})
            user_id, email = _create_user(admin, realm_admin, "undeliverable")
            failed = _send_action_email(admin, realm_admin, user_id)
            if failed.status_code != 500 or "Failed to send" not in failed.text:
                raise E2EError(
                    f"an unreachable SMTP server was not reported as a delivery failure: HTTP {failed.status_code}"
                )
            if email in _sink_recipients(cfg):
                raise E2EError(f"the mail sink holds a message for {email}, whose delivery was reported as failed")
        finally:
            with contextlib.suppress(requests.RequestException):
                _call(admin, "DELETE", realm_admin)
