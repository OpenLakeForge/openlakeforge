"""Identity checks (#24): the issuer answers and puts the canonical role in the token.

The role check drives the real browser flow against the real `perimeter`
client, so it exercises the client's secret and group mapper rather than a
test-only client. Test users are created through the admin API and always
deleted; user records never live in Terraform (ADR 0014).
"""

from __future__ import annotations

import base64
import contextlib
import html
import json
import os
import re
import secrets
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
import yaml

from olf import access, config, k8s, log
from olf.contracts import CONTRACT_STAGE_ENV
from olf.e2e._shell import E2EConfig, E2EError, kubectl, load_provider_contracts_or_raise

KEYCLOAK_SERVICE = "keycloak"
KEYCLOAK_ADMIN_SECRET = "keycloak-admin-creds"
REQUEST_TIMEOUT = 15
_LOGIN_ACTION = re.compile(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"')


def check_identity(cfg: E2EConfig) -> None:
    identity = load_provider_contracts_or_raise(cfg)["shared"]["identity"]
    if identity.get("implementation") != "identity.oidc" or identity.get("adapter") != "keycloak":
        log.info("Skipping the identity checks: the Keycloak adapter is not deployed.")
        return
    if config.truthy(os.environ.get(access.PORT_FORWARD_ENV, "")):
        log.info("Skipping the identity checks: OLF_PORT_FORWARD bypasses the ingress the issuer URL needs.")
        return
    stage = os.environ.get(CONTRACT_STAGE_ENV, "dev")
    with access.service_url(
        "shared/identity",
        service=KEYCLOAK_SERVICE,
        remote_port=8080,
        namespace=cfg.platform_namespace,
        log_path=f"/tmp/openlakeforge-{cfg.env}-keycloak-port-forward.log",
        shared_namespace=cfg.platform_namespace,
        kube_context=cfg.kube_context,
    ) as base_url:
        check_discovery(identity, base_url)
        check_discovery_from_pod(cfg, identity)
        redirect_uri = f"{_stage_route(cfg, stage)}/oauth2/callback"
        check_client_secret_is_enforced(cfg, identity, redirect_uri)
        check_role_claims(cfg, identity, base_url, redirect_uri)


def _stage_route(cfg: E2EConfig, stage: str) -> str:
    contracts = load_provider_contracts_or_raise(cfg)
    try:
        return str(contracts["shared"]["access"]["routes"][f"stage/{stage}/orchestration"]["url"])
    except KeyError as exc:
        raise E2EError(f"the contract has no route for stage {stage!r}'s orchestration service") from exc


def check_discovery(identity: dict[str, Any], base_url: str) -> None:
    log.step("Checking the issuer discovery document from the host...")
    issuer = identity["issuer_url"]
    if not issuer.startswith(base_url):
        raise E2EError(f"issuer_url {issuer} is not served by the identity route {base_url}")
    response = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if response.json()["issuer"] != issuer:
        raise E2EError(f"discovery document says iss {response.json()['issuer']!r}, contract says {issuer!r}")


def check_discovery_from_pod(cfg: E2EConfig, identity: dict[str, Any]) -> None:
    """A pod resolves the same https URL, trusts its certificate and sees the same `iss`.

    This is the back-channel a service takes to fetch the token and JWKS
    endpoints, which `*.localhost` would otherwise send to the pod's own
    loopback.
    """
    log.step("Checking the issuer discovery document from inside a pod...")
    catalog = yaml.safe_load((cfg.distribution_root / "release/component-catalog.yaml").read_text())
    image = catalog["components"]["images"]["k8s_bootstrap"]
    issuer = identity["issuer_url"]
    pod = "olf-e2e-issuer-probe"
    url = f"{issuer}/.well-known/openid-configuration"
    host = urlsplit(issuer).hostname
    # curl itself treats *.localhost as loopback (RFC 6761) without asking DNS,
    # so the address cluster DNS answers is looked up and handed to it;
    # getaddrinfo-based clients (Python, Java) need no such help.
    script = (
        f'ip="$(getent hosts {host} | cut -d" " -f1)" && '
        f'curl -sS --fail --cacert /ca/ca.crt --resolve {host}:443:"$ip" {url}'
    )
    spec = {
        "containers": [
            {
                "name": pod,
                "image": image,
                "command": ["sh", "-c", script],
                "volumeMounts": [{"name": "ca", "mountPath": "/ca"}],
            }
        ],
        "volumes": [{"name": "ca", "configMap": {"name": "olf-local-ca"}}],
    }
    kubectl(cfg, ["delete", "pod", pod, "-n", cfg.platform_namespace, "--ignore-not-found"])
    out = kubectl(
        cfg,
        [
            "run",
            pod,
            "-n",
            cfg.platform_namespace,
            "--rm",
            "-i",
            "--restart=Never",
            f"--image={image}",
            f"--overrides={json.dumps({'spec': spec})}",
        ],
        capture=True,
    )
    served = json.loads(out[out.index("{") : out.rindex("}") + 1])["issuer"]
    if served != issuer:
        raise E2EError(f"a pod sees iss {served!r}, the contract says {issuer!r}")


def _client_secret(cfg: E2EConfig, identity: dict[str, Any], client: str) -> str:
    ref = identity["clients"][client]["secret_ref"]
    return k8s.secret_value(ref["name"], ref["key"], cfg.platform_namespace, kube_context=cfg.kube_context)


def check_client_secret_is_enforced(cfg: E2EConfig, identity: dict[str, Any], redirect_uri: str) -> None:
    """The Secret a client references is the credential the issuer enforces.

    A wrong code with the right secret is `invalid_grant`; a wrong secret is
    `unauthorized_client` whatever the code. Rotation (docs/setup/identity-secret-rotation.md)
    changes the first into the second for the old value.
    """
    log.step("Checking each client's referenced Secret authenticates it...")
    token_url = f"{identity['issuer_url']}/protocol/openid-connect/token"
    for client, spec in identity["clients"].items():
        outcomes = {}
        for label, value in (("referenced", _client_secret(cfg, identity, client)), ("wrong", "not-the-secret")):
            reply = requests.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": "invalid",
                    "redirect_uri": redirect_uri,
                    "client_id": spec["client_id"],
                    "client_secret": value,
                },
                timeout=REQUEST_TIMEOUT,
            )
            outcomes[label] = reply.json().get("error")
        if outcomes != {"referenced": "invalid_grant", "wrong": "unauthorized_client"}:
            raise E2EError(f"client {client}: expected the referenced Secret to authenticate, got {outcomes}")


@contextlib.contextmanager
def _admin_session(cfg: E2EConfig, base_url: str) -> Iterator[requests.Session]:
    password = k8s.secret_value(
        KEYCLOAK_ADMIN_SECRET, "password", cfg.platform_namespace, kube_context=cfg.kube_context
    )
    reply = requests.post(
        f"{base_url}/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    reply.raise_for_status()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {reply.json()['access_token']}"
    yield session


def _admin(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    reply = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    reply.raise_for_status()
    return reply


def _id_token_claims(
    identity: dict[str, Any], client: dict[str, str], secret: str, redirect_uri: str, user: str, password: str
) -> dict[str, Any]:
    """Log in through the browser flow and return the ID token's claims."""
    browser = requests.Session()
    page = browser.get(
        f"{identity['issuer_url']}/protocol/openid-connect/auth",
        params={
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid",
            "state": "e2e",
        },
        timeout=REQUEST_TIMEOUT,
    )
    page.raise_for_status()
    match = _LOGIN_ACTION.search(page.text)
    if match is None:
        raise E2EError("the issuer's login page has no login form")
    login = browser.post(
        html.unescape(match.group(1)),
        data={"username": user, "password": password},
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    code = parse_qs(urlsplit(login.headers.get("Location", "")).query).get("code")
    if login.status_code != 302 or not code:
        raise E2EError(
            f"login as {user} did not return an authorization code "
            f"(HTTP {login.status_code}, Location {login.headers.get('Location')})"
        )
    token = requests.post(
        f"{identity['issuer_url']}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "code": code[0],
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "client_secret": secret,
        },
        timeout=REQUEST_TIMEOUT,
    )
    token.raise_for_status()
    # Straight from the token endpoint over TLS, so the claims are read, not verified.
    payload = token.json()["id_token"].split(".")[1]
    return dict(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))))


def check_role_claims(cfg: E2EConfig, identity: dict[str, Any], base_url: str, redirect_uri: str) -> None:
    """A throwaway user per canonical role gets exactly that role in the role claim;
    a user in no group gets none."""
    log.step("Checking each canonical role reaches the ID token...")
    claim = identity["role_claim"]
    realm_admin = f"{base_url}/admin/realms/{identity['provider']['realm']}"
    secret = _client_secret(cfg, identity, "perimeter")
    suffix = secrets.token_hex(3)
    created: list[str] = []
    try:
        with _admin_session(cfg, base_url) as admin:
            for role in [*identity["role_mapping"], None]:
                name = f"olf-e2e-{role or 'no-role'}-{suffix}"
                password = secrets.token_urlsafe(24)
                _admin(
                    admin,
                    "POST",
                    f"{realm_admin}/users",
                    json={
                        "username": name,
                        "enabled": True,
                        "emailVerified": True,
                        "email": f"{name}@example.invalid",
                        "firstName": "OLF",
                        "lastName": "E2E",
                        "credentials": [{"type": "password", "value": password, "temporary": False}],
                    },
                )
                user_id = _admin(
                    admin, "GET", f"{realm_admin}/users", params={"username": name, "exact": "true"}
                ).json()[0]["id"]
                created.append(user_id)
                if role is not None:
                    group = identity["role_mapping"][role][0]
                    group_id = _admin(
                        admin, "GET", f"{realm_admin}/groups", params={"search": group, "exact": "true"}
                    ).json()[0]["id"]
                    _admin(admin, "PUT", f"{realm_admin}/users/{user_id}/groups/{group_id}")
                claims = _id_token_claims(
                    identity, identity["clients"]["perimeter"], secret, redirect_uri, name, password
                )
                expected = identity["role_mapping"][role] if role is not None else []
                if claims.get(claim, []) != expected:
                    raise E2EError(f"{name}: expected {claim}={expected}, got {claims.get(claim)!r}")
                if claims["iss"] != identity["issuer_url"]:
                    raise E2EError(f"{name}: token iss {claims['iss']!r} differs from the contract's issuer_url")
    finally:
        if created:
            with _admin_session(cfg, base_url) as admin:
                for user_id in created:
                    with contextlib.suppress(requests.RequestException):
                        _admin(admin, "DELETE", f"{realm_admin}/users/{user_id}")
