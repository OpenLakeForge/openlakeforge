"""A browser session reaches the platform API with its cookie alone (#799).

The perimeter's proxy returns the session's ID token as `Authorization` on the
`/api` route only, and the API accepts only audience `olf-cli`: a 200 from
`whoami` naming that audience shows Dex's cross-client token went through the
whole path. A write from another origin must be refused before any token is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests

from olf.common import log
from olf.infra.e2e._cli_session import CLI_CLIENT
from olf.infra.e2e._identity_github import REQUEST_TIMEOUT
from olf.infra.e2e._shell import E2EError

CROSS_ORIGIN = "https://evil.example"


def check_browser_session(identity: Mapping[str, Any], api_url: str, role: str, browser: requests.Session) -> None:
    """`browser` holds a signed-in session; call the API at `api_url` (the portal's origin) as a page would."""
    log.step(f"Checking the browser session reaches the platform API as {role}...")
    base = api_url.rstrip("/")
    reply = browser.get(f"{base}/api/v1/whoami", headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT)
    who = reply.json() if reply.status_code == 200 else {}
    expected = identity["clients"][CLI_CLIENT]["client_id"]
    if who.get("role") != role or who.get("audience") != expected:
        raise E2EError(
            f"{base}/api/v1/whoami with the session cookie only: HTTP {reply.status_code} {reply.text[:200]}; "
            f"expected role {role} and audience {expected}"
        )
    # A stage that may not exist is fine: the origin check runs before the route's own checks.
    write = browser.post(
        f"{base}/api/v1/stages/dev/runs",
        json={},
        headers={"Accept": "application/json", "Origin": CROSS_ORIGIN},
        timeout=REQUEST_TIMEOUT,
    )
    if write.status_code != 403 or "cross-origin" not in write.text:
        raise E2EError(f"a POST from {CROSS_ORIGIN} with the session got HTTP {write.status_code} {write.text[:200]}")
