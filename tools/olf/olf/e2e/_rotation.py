"""Workload-secret rotation drill (#178): `olf secrets rotate` on the perimeter's Dex client.

The command regenerates the Secret and restarts Dex and the perimeter. The
drill then signs in through the portal, which succeeds only when Dex and
oauth2-proxy hold the same new secret. Only the Secret's uid and
resourceVersion are read, never its value.
"""

from __future__ import annotations

import os
import secrets
import time
from urllib.parse import urlsplit

from olf import access, config, log, secrets_rotation
from olf.deployment.engine import Toolkit
from olf.e2e._identity_github import dex_points_at_stub, github_stub, log_skip, sign_in_with_stub, stub_host
from olf.e2e._perimeter import DENIAL_HEADING, PORTAL_REF
from olf.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise

TARGET = "dex-client-perimeter"


def check_secret_rotation(cfg: E2EConfig) -> None:
    contracts = load_provider_contracts_or_raise(cfg)
    access_contract, identity = contracts["shared"]["access"], contracts["shared"]["identity"]
    perimeter = access_contract.get("perimeter")
    if cfg.deployment is None or not perimeter or not perimeter["enabled"] or identity.get("adapter") != "dex":
        log.info("Skipping the secret rotation drill: it needs Dex, the perimeter and a resolved deployment.")
        return
    if config.truthy(os.environ.get(access.PORT_FORWARD_ENV, "")):
        log.info("Skipping the secret rotation drill: OLF_PORT_FORWARD bypasses the ingress it signs in through.")
        return
    if not dex_points_at_stub(cfg):
        log_skip()
        return
    log.step(f"Rotating {TARGET} with `olf secrets rotate`, then signing in...")
    started = time.monotonic()
    kubectl = Toolkit.default().kubectl
    secrets_rotation.rotate_client(cfg.deployment, TARGET, kubectl=kubectl)
    mapping = identity["role_mapping"]
    role = next((r for r in identity["roles"]["precedence"] if r in mapping), None)
    if role is None:
        raise E2EError("the profile maps no team to any role; the sign-in after rotation would admit nobody")
    login = f"olf-e2e-rotation-{secrets.token_hex(3)}"
    portal = access_contract["routes"][PORTAL_REF]["url"]
    with access.service_url(
        "shared/identity",
        service="dex",
        remote_port=5556,
        namespace=cfg.platform_namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-dex-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
    ):
        with github_stub(cfg, {login: mapping[role]}):
            browser, landing = sign_in_with_stub(portal, login, stub_host(cfg))
    session = [c.name for c in browser.cookies if c.name.startswith("_oauth2_proxy") and "csrf" not in c.name]
    if (
        landing.status_code >= 400
        or urlsplit(landing.url).hostname != urlsplit(portal).hostname
        or DENIAL_HEADING in landing.text
        or not session
    ):
        raise E2EError(
            f"sign-in after rotating {TARGET} failed: HTTP {landing.status_code} at {landing.url}, "
            f"session cookie set: {bool(session)}"
        )
    log.info(f"Rotated {TARGET} and signed in through the portal in {time.monotonic() - started:.0f}s.")
