"""The `olf-users` admin client (#331): it authenticates by client credentials
and administers users, and nothing wider.

Its roles are the least that list, disable and enable users and move them
between the canonical-role groups; a realm setting, client, event or other
realm is outside them. The checks below fail if a role is ever added.
"""

from __future__ import annotations

from typing import Any

import requests

from olf import k8s, log
from olf.e2e._shell import E2EConfig, E2EError

REQUEST_TIMEOUT = 15


def check_admin_client(cfg: E2EConfig, identity: dict[str, Any], base_url: str, master_admin: requests.Session) -> None:
    log.step("Checking the olf-users admin client authenticates and is limited to user administration...")
    admin = identity.get("admin")
    if not identity.get("capabilities", {}).get("admin_api") or admin is None:
        raise E2EError("the Keycloak adapter must declare capabilities.admin_api and emit shared.identity.admin")
    realm = str(identity["issuer_url"].rsplit("/", 1)[1])
    ref = admin["secret_ref"]
    secret = k8s.secret_value(ref["name"], ref["key"], cfg.platform_namespace, kube_context=cfg.kube_context)
    token_url = f"{base_url}/realms/{realm}/protocol/openid-connect/token"
    credentials = {"client_id": admin["client_id"], "client_secret": secret}

    wrong = requests.post(
        token_url,
        data={"grant_type": "client_credentials", **credentials, "client_secret": "not-the-secret"},
        timeout=REQUEST_TIMEOUT,
    )
    if wrong.status_code != 401:
        raise E2EError(f"olf-users with a wrong secret got HTTP {wrong.status_code}, expected 401")
    password = requests.post(
        token_url,
        data={"grant_type": "password", **credentials, "username": "x", "password": "x"},
        timeout=REQUEST_TIMEOUT,
    )
    # Keycloak refuses a client without direct access grants before it looks at the user, so `x`/`x` still gets
    # this error; a bad-credentials answer (401 invalid_grant) would mean the grant is enabled.
    if password.status_code != 400 or password.json().get("error") != "unauthorized_client":
        raise E2EError(f"olf-users must not be a login client, but the password grant got HTTP {password.status_code}")

    token = requests.post(token_url, data={"grant_type": "client_credentials", **credentials}, timeout=REQUEST_TIMEOUT)
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    realm_admin = f"{base_url}/admin/realms/{realm}"

    def status(method: str, url: str, **kwargs: Any) -> int:
        return requests.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs).status_code

    for path in ("users", "groups"):
        if (code := status("GET", f"{realm_admin}/{path}")) != 200:
            raise E2EError(f"olf-users could not read {path}: HTTP {code}")
    users = requests.get(f"{realm_admin}/users", headers=headers, timeout=REQUEST_TIMEOUT).json()
    if any(user["username"].startswith("service-account-") for user in users):
        raise E2EError("the user listing includes a service account; olf users would offer to grant it a role")

    # The read-only checks above pass with view-users alone; the client exists to write users.
    created = requests.post(
        f"{realm_admin}/users",
        headers=headers,
        json={"username": "olf-e2e-admin-probe", "enabled": False},
        timeout=REQUEST_TIMEOUT,
    )
    if created.status_code != 201:
        raise E2EError(f"olf-users could not create a user: HTTP {created.status_code}")
    removed = status("DELETE", f"{realm_admin}/users/{created.headers['Location'].rsplit('/', 1)[1]}")
    if removed != 204:
        raise E2EError(f"olf-users created the user olf-e2e-admin-probe but could not delete it: HTTP {removed}")

    # Any realm-management role gets Keycloak's brief realm representation (HTTP
    # 200); the settings (mail server, lifespans, events) must stay hidden.
    brief = requests.get(realm_admin, headers=headers, timeout=REQUEST_TIMEOUT)
    if brief.status_code == 200 and {"smtpServer", "accessTokenLifespan", "adminEventsEnabled"} & set(brief.json()):
        raise E2EError("olf-users must not read the realm settings, but the realm representation carries them")

    forbidden = {
        "write the realm settings": ("PUT", realm_admin),
        "list clients": ("GET", f"{realm_admin}/clients"),
        "read admin events": ("GET", f"{realm_admin}/admin-events"),
        "list another realm's users": ("GET", f"{base_url}/admin/realms/master/users"),
    }
    for what, (method, url) in forbidden.items():
        code = status(method, url, **({"json": {}} if method == "PUT" else {}))
        if code not in (401, 403):
            raise E2EError(f"olf-users must not {what}, but got HTTP {code}")

    # Admin writes are recorded, without request bodies that could carry a credential.
    realm_settings = master_admin.get(realm_admin, timeout=REQUEST_TIMEOUT).json()
    if realm_settings.get("adminEventsEnabled") is not True or realm_settings.get("adminEventsDetailsEnabled"):
        raise E2EError("the realm must record admin events with details off")
    events = master_admin.get(f"{realm_admin}/admin-events", params={"max": 20}, timeout=REQUEST_TIMEOUT).json()
    if not events:
        raise E2EError("no admin event was recorded, though the realm Job and the e2e wrote to the realm")
    if any(event.get("representation") for event in events):
        raise E2EError("an admin event carries a representation; details must stay off")
