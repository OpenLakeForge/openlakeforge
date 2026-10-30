"""Perimeter checks (#176): authentication and role authorization at the ingress.

These drive the public hostnames on purpose: they test the perimeter itself,
signing in through Dex's GitHub connector against a stub as per-role users
(docs/architecture/identity/identity-callers.md).
Every other e2e path reaches services by port-forward and never sees the
perimeter (`olf.infra.access.service_url`).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

from olf.common import config, log
from olf.infra import access
from olf.infra.e2e._identity_github import (
    REQUEST_TIMEOUT,
    dex_points_at_stub,
    github_stub,
    log_skip,
    sign_in_with_stub,
    stub_host,
)
from olf.infra.e2e._perimeter_bearer import check_bearer_tokens
from olf.infra.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise

PORTAL_REF = "shared/portal"
DENIAL_HEADING = "Access is role-restricted"
# What a caller outside the cluster could put on a request to look like the
# proxy's own answer. None of it may change a decision.
FORGED_HEADERS = {
    "X-Forwarded-User": "admin",
    "X-Forwarded-Email": "admin@example.invalid",
    "X-Forwarded-Groups": "platform-admin",
    "X-Auth-Request-User": "admin",
    "X-Auth-Request-Email": "admin@example.invalid",
    "X-Auth-Request-Groups": "platform-admin",
}


def check_perimeter(cfg: E2EConfig) -> None:
    contracts = load_provider_contracts_or_raise(cfg)
    access_contract = contracts["shared"]["access"]
    identity = contracts["shared"]["identity"]
    perimeter = access_contract.get("perimeter")
    if not perimeter or not perimeter["enabled"]:
        log.info("Skipping the perimeter checks: the contract declares no enabled perimeter.")
        return
    if identity.get("adapter") != "dex":
        log.info("Skipping the perimeter checks: they need the Dex adapter.")
        return
    if config.truthy(os.environ.get(access.PORT_FORWARD_ENV, "")):
        log.info("Skipping the perimeter checks: OLF_PORT_FORWARD bypasses the ingress they test.")
        return
    routes = {ref: route["url"] for ref, route in access_contract["routes"].items()}
    # The context also trusts the local CA and pins the ingress hostnames the requests need.
    with access.service_url(
        "shared/identity",
        service="dex",
        remote_port=5556,
        namespace=cfg.platform_namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-dex-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
    ):
        _check_dex_perimeter(cfg, identity, perimeter["protected_routes"], routes)


def _check_dex_perimeter(
    cfg: E2EConfig, identity: Mapping[str, Any], protected: list[str], routes: Mapping[str, str]
) -> None:
    """Sign in through Dex's GitHub connector against the stub and check every role's decisions."""
    if not dex_points_at_stub(cfg):
        log_skip()
        return
    suffix = secrets.token_hex(3)
    mapping = identity["role_mapping"]
    # Every mapped role, platform-admin included, so the check always signs in
    # at least one admitted user and cannot pass on refusals alone.
    precedence = identity["roles"]["precedence"]
    roles: list[str | None] = [r for r in precedence if r in mapping]
    if not roles:
        raise E2EError("the profile maps no team to any role; the Dex checks would admit nobody")
    for missing in (r for r in precedence if r not in mapping):
        log.info(f"Skipping the {missing} role: the profile maps no team to it.")
    # A user in no mapped team is still in the organisation, in a team that maps to no role.
    organization = next(iter(mapping.values()))[0].split(":")[0]
    roles.append(None)
    logins = {role: f"olf-e2e-{role or 'no-team'}-{suffix}" for role in roles}
    teams = {logins[r]: mapping[r] if r else [f"{organization}:olf-e2e-unmapped"] for r in roles}
    check_unauthenticated_requests(identity, protected, routes, login=f"{identity['issuer_url']}/auth")

    def sign_in(url: str, login: str) -> tuple[requests.Session, requests.Response]:
        return sign_in_with_stub(url, login, stub_host(cfg))

    with github_stub(cfg, teams):
        for role, login in logins.items():
            check_role_decisions(identity, protected, routes, role, login, sign_in)
        check_bearer_tokens(cfg, identity, routes, logins, lambda ref, role: _grants(identity, ref, role))


def check_unauthenticated_requests(
    identity: Mapping[str, Any], protected: list[str], routes: Mapping[str, str], login: str
) -> None:
    """No session: a browser is sent to the issuer, an API caller gets 401, forged headers change nothing."""
    log.step("Checking unauthenticated requests are refused at every protected route...")
    for ref in protected:
        for label, headers in (("", {}), (" with forged identity headers", FORGED_HEADERS)):
            reply = requests.get(routes[ref], headers=headers, allow_redirects=False, timeout=REQUEST_TIMEOUT)
            if reply.status_code != 302 or not reply.headers.get("Location", "").startswith(login):
                raise E2EError(
                    f"{ref}: an unauthenticated browser request{label} got HTTP {reply.status_code} "
                    f"(Location {reply.headers.get('Location')}), expected a redirect to {login}"
                )
        api = requests.get(
            routes[ref], headers={"Accept": "application/json"}, allow_redirects=False, timeout=REQUEST_TIMEOUT
        )
        if api.status_code != 401:
            raise E2EError(f"{ref}: an unauthenticated API request got HTTP {api.status_code}, expected 401")


def check_role_decisions(
    identity: Mapping[str, Any],
    protected: list[str],
    routes: Mapping[str, str],
    role: str | None,
    username: str,
    sign_in: Callable[[str, str], tuple[requests.Session, requests.Response]],
) -> None:
    """Signed in with `role` (None: no role), every protected route answers as the role model says.

    A user with no role is turned away by Dex itself, before any token exists.
    """
    log.step(f"Checking route decisions for {role or 'a user with no role'}...")
    browser, landing = sign_in(routes[PORTAL_REF], username)
    if role is None:
        _require_refused_by_issuer(identity, browser, landing, username)
        return
    # A denied request makes the proxy clear the cookie in the browser; keep the session for the direct calls.
    cookies = browser.cookies.copy()
    for ref in protected:
        if _grants(identity, ref, role):
            reply = browser.get(routes[ref], timeout=REQUEST_TIMEOUT)
            if reply.status_code >= 400 or DENIAL_HEADING in reply.text:
                raise E2EError(f"{role} should reach {ref}, got HTTP {reply.status_code} at {reply.url}")
            continue
        for label, headers in (("", {}), (" with forged headers", FORGED_HEADERS)):
            reply = browser.get(routes[ref], headers=headers, timeout=REQUEST_TIMEOUT)
            _require_denial(reply, f"{role} at {ref}{label}", routes[PORTAL_REF])
        # A request that skips the UI: the session cookie alone, an API path, no redirect to follow.
        direct = requests.get(
            f"{routes[ref]}/api/v1/",
            cookies=cookies,
            headers={"Accept": "application/json", **FORGED_HEADERS},
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
        _require_denial(direct, f"{role} calling the API of {ref} directly", routes[PORTAL_REF])


def _require_refused_by_issuer(
    identity: Mapping[str, Any], browser: requests.Session, landing: requests.Response, username: str
) -> None:
    """Dex itself turned the login away, not the proxy after a sign-in.

    Matched on: the final response is an error served by the issuer's host at
    its `/callback` (where Dex renders a failed connector login), and no
    oauth2-proxy session cookie was set (`_csrf` is set before sign-in, so it is ignored).
    """
    issuer_host = urlsplit(identity["issuer_url"]).hostname
    landed = urlsplit(landing.url)
    session = [c.name for c in browser.cookies if c.name.startswith("_oauth2_proxy") and "csrf" not in c.name]
    if landing.status_code < 400 or landed.hostname != issuer_host or landed.path != "/callback" or session:
        raise E2EError(
            f"{username} (no mapped team) was not refused by the issuer at login: HTTP {landing.status_code} "
            f"at {landing.url}, session cookies {session}"
        )


def _grants(identity: Mapping[str, Any], ref: str, role: str) -> bool:
    service = ref.rsplit("/", 1)[1]
    return service == "portal" or role in identity["roles"]["grants"].get(service, {})


def _require_denial(reply: requests.Response, who: str, portal_url: str) -> None:
    if reply.status_code != 403 or DENIAL_HEADING not in reply.text or f'href="{portal_url}"' not in reply.text:
        raise E2EError(
            f"{who}: expected a 403 saying {DENIAL_HEADING!r} with a link to {portal_url}, "
            f"got HTTP {reply.status_code} at {reply.url}"
        )
