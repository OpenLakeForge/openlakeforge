"""Perimeter checks (#176): authentication and role authorization at the ingress.

These drive the public hostnames on purpose: they test the perimeter itself,
signing in over HTTP as per-role throwaway users (docs/architecture/identity-callers.md).
Every other e2e path reaches services by port-forward and never sees the
perimeter (`olf.access.service_url`).
"""

from __future__ import annotations

import html
import os
import secrets
from collections.abc import Callable, Mapping
from typing import Any

import requests

from olf import access, config, log
from olf.e2e._identity import KEYCLOAK_SERVICE, LOGIN_ACTION, REQUEST_TIMEOUT, throwaway_users
from olf.e2e._identity_github import dex_points_at_stub, github_stub, log_skip, sign_in_with_stub, stub_host
from olf.e2e._shell import E2EConfig, E2EError, load_provider_contracts_or_raise

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
    if identity.get("adapter") not in ("keycloak", "dex"):
        log.info("Skipping the perimeter checks: they need the Keycloak or the Dex adapter.")
        return
    if config.truthy(os.environ.get(access.PORT_FORWARD_ENV, "")):
        log.info("Skipping the perimeter checks: OLF_PORT_FORWARD bypasses the ingress they test.")
        return
    routes = {ref: route["url"] for ref, route in access_contract["routes"].items()}
    if identity["adapter"] == "dex":
        _check_dex_perimeter(cfg, identity, perimeter["protected_routes"], routes)
        return
    with access.service_url(
        "shared/identity",
        service=KEYCLOAK_SERVICE,
        remote_port=8080,
        namespace=cfg.platform_namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-keycloak-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
    ) as base_url:
        check_unauthenticated_requests(identity, perimeter["protected_routes"], routes)
        with throwaway_users(cfg, identity, base_url, ["viewer", "analyst", "data-engineer", None]) as users:
            for role, (username, password) in users.items():
                check_role_decisions(identity, perimeter["protected_routes"], routes, role, username, password)


def _check_dex_perimeter(
    cfg: E2EConfig, identity: Mapping[str, Any], protected: list[str], routes: Mapping[str, str]
) -> None:
    """The same decisions with Dex as issuer, signing in through its GitHub connector against the stub."""
    if not dex_points_at_stub(cfg):
        log_skip()
        return
    suffix = secrets.token_hex(3)
    roles: list[str | None] = ["viewer", "analyst", "data-engineer", None]
    # A user in no mapped team is still in the organisation, in a team that maps to no role.
    organization = identity["role_mapping"]["viewer"][0].split(":")[0]
    logins = {role: f"olf-e2e-{role or 'no-team'}-{suffix}" for role in roles}
    teams = {logins[r]: identity["role_mapping"][r] if r else [f"{organization}:olf-e2e-unmapped"] for r in roles}
    check_unauthenticated_requests(identity, protected, routes, login=f"{identity['issuer_url']}/auth")

    def sign_in(url: str, login: str, _password: str) -> tuple[requests.Session, requests.Response]:
        return sign_in_with_stub(url, login, stub_host(cfg))

    with github_stub(cfg, teams):
        for role, login in logins.items():
            check_role_decisions(identity, protected, routes, role, login, "", sign_in=sign_in, refused_at_login=True)


def check_unauthenticated_requests(
    identity: Mapping[str, Any], protected: list[str], routes: Mapping[str, str], login: str | None = None
) -> None:
    """No session: a browser is sent to the issuer, an API caller gets 401, forged headers change nothing."""
    log.step("Checking unauthenticated requests are refused at every protected route...")
    login = login or f"{identity['issuer_url']}/protocol/openid-connect/auth"
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
    password: str,
    *,
    sign_in: Callable[[str, str, str], tuple[requests.Session, requests.Response]] | None = None,
    refused_at_login: bool = False,
) -> None:
    """Signed in with `role` (None: no role), every protected route answers as the role model says.

    `refused_at_login`: the issuer itself turns away a user with no role (Dex
    does, before any token exists) instead of the proxy denying them after sign-in.
    """
    log.step(f"Checking route decisions for {role or 'a user with no role'}...")
    browser, landing = (sign_in or _sign_in)(routes[PORTAL_REF], username, password)
    if role is None:
        if not refused_at_login:
            _require_denial(landing, f"{username} (no role) signing in", routes[PORTAL_REF])
        elif landing.status_code < 400:
            raise E2EError(f"{username} (no mapped team) was not refused at login: HTTP {landing.status_code}")
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


def _grants(identity: Mapping[str, Any], ref: str, role: str) -> bool:
    service = ref.rsplit("/", 1)[1]
    return service == "portal" or role in identity["roles"]["grants"].get(service, {})


def _require_denial(reply: requests.Response, who: str, portal_url: str) -> None:
    if reply.status_code != 403 or DENIAL_HEADING not in reply.text or f'href="{portal_url}"' not in reply.text:
        raise E2EError(
            f"{who}: expected a 403 saying {DENIAL_HEADING!r} with a link to {portal_url}, "
            f"got HTTP {reply.status_code} at {reply.url}"
        )


def _sign_in(url: str, username: str, password: str) -> tuple[requests.Session, requests.Response]:
    """Open `url`, follow the redirect to the issuer's login form and submit it; the session keeps the cookie."""
    browser = requests.Session()
    page = browser.get(url, timeout=REQUEST_TIMEOUT)
    form = LOGIN_ACTION.search(page.text)
    if form is None:
        raise E2EError(f"{url} did not lead to the issuer's login form (HTTP {page.status_code} at {page.url})")
    landing = browser.post(
        html.unescape(form.group(1)), data={"username": username, "password": password}, timeout=REQUEST_TIMEOUT
    )
    return browser, landing
