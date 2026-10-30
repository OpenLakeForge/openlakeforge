"""Bearer-token checks at the perimeter (#536): a token issued to the public
`olf-cli` client is accepted on a protected API route, and the role floor still decides.

The token comes from the same stub sign-in as the browser checks, through the
authorization-code flow with PKCE. The loopback redirect has no listener: the
authorization code is read off the redirect instead.
"""

from __future__ import annotations

import base64
import hashlib
import io
import secrets
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from olf.common import log
from olf.infra import k8s
from olf.infra.e2e._identity_github import REQUEST_TIMEOUT, StubAuthorize, stub_host
from olf.infra.e2e._shell import E2EConfig, E2EError

CLI_CLIENT = "olf-cli"
# Dagster has no authorization of its own, so a 2xx here is the perimeter's decision alone. `/graphql` is
# an API route (401, never a login redirect, without a session).
API_SERVICE = "orchestration"
API_PATH = "/graphql?query=%7Bversion%7D"
SCOPE = "openid email profile groups"


class _LoopbackRedirect(requests.adapters.BaseAdapter):
    """Stands in for the client's loopback listener: answers 200 so the redirect ends with its URL."""

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        reply = requests.Response()
        reply.status_code = 200
        reply.url = str(request.url)
        reply.request = request
        reply.raw = io.BytesIO(b"")
        return reply

    def close(self) -> None:
        pass


def issue_id_token(
    cfg: E2EConfig,
    identity: Mapping[str, Any],
    login: str,
    client_id: str,
    redirect_uri: str,
    client_secret: str | None = None,
) -> str:
    """Sign in as `login` at the stub and redeem the code for an ID token issued to `client_id`."""
    issuer = identity["issuer_url"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(8)
    redirect = urlsplit(redirect_uri)
    browser = requests.Session()
    browser.mount(f"https://{stub_host(cfg)}", StubAuthorize(login))
    browser.mount(f"{redirect.scheme}://{redirect.netloc}", _LoopbackRedirect())
    landed = browser.get(
        f"{issuer}/auth",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        timeout=REQUEST_TIMEOUT,
    )
    query = parse_qs(urlsplit(landed.url).query)
    if "code" not in query or query.get("state") != [state]:
        raise E2EError(
            f"{login}: the {client_id} authorization did not end at {redirect_uri} with a code: {landed.url}"
        )
    form = {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    reply = requests.post(f"{issuer}/token", data=form, timeout=REQUEST_TIMEOUT)
    if reply.status_code != 200 or "id_token" not in reply.json():
        raise E2EError(f"{login}: the {client_id} token exchange got HTTP {reply.status_code}")
    return str(reply.json()["id_token"])


def tamper_signature(token: str) -> str:
    """Change a character in the middle of the signature, where every bit is significant."""
    head, signature = token.rsplit(".", 1)
    middle = len(signature) // 2
    return f"{head}.{signature[:middle]}{'A' if signature[middle] != 'A' else 'B'}{signature[middle + 1 :]}"


def check_bearer_decisions(
    url: str,
    roles: list[str],
    admits: Callable[[str], bool],
    cli_token: Callable[[str], str],
    other_token: Callable[[str], str] | None,
) -> None:
    """`url` is a protected API route: `roles` get 2xx at or above its floor and 403 below it, and no
    token, a tampered signature and another client's token get 401."""

    def call(token: str | None) -> int:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return requests.get(url, headers=headers, allow_redirects=False, timeout=REQUEST_TIMEOUT).status_code

    def require(who: str, got: int, ok: bool) -> None:
        if not ok:
            raise E2EError(f"{who} calling {url}: got HTTP {got}")

    admitted = [role for role in roles if admits(role)]
    if not admitted:
        raise E2EError("no mapped role is admitted to the bearer route; the check would pass on refusals alone")
    for role in roles:
        status = call(cli_token(role))
        who = f"an {CLI_CLIENT} token for {role}"
        if admits(role):
            require(f"{who} (at or above the floor, expected 2xx)", status, 200 <= status < 300)
        else:
            require(f"{who} (below the floor, expected 403)", status, status == 403)
    cases = [
        ("no token", None),
        (f"an {CLI_CLIENT} token with a tampered signature", tamper_signature(cli_token(admitted[0]))),
    ]
    if other_token is not None:
        cases.append(("a token issued to another client", other_token(admitted[0])))
    for who, token in cases:
        status = call(token)
        require(f"{who} (expected 401)", status, status == 401)


def check_bearer_tokens(
    cfg: E2EConfig,
    identity: Mapping[str, Any],
    routes: Mapping[str, str],
    logins: Mapping[str | None, str],
    admits: Callable[[str, str], bool],
) -> None:
    """Needs the stub users already running, one per mapped role (`logins`, None is the user in no team)."""
    log.step(f"Checking {CLI_CLIENT} bearer tokens at the perimeter...")
    refs = [ref for ref in routes if ref.rsplit("/", 1)[-1] == API_SERVICE]
    if CLI_CLIENT not in identity.get("clients", {}) or not refs:
        log.info(f"Skipping the bearer checks: the contract has no {CLI_CLIENT} client or no {API_SERVICE} route.")
        return
    ref = refs[0]
    roles = [role for role in logins if role is not None]
    # RFC 8252 loopback redirect: Dex accepts any port, none is registered.
    loopback = f"http://127.0.0.1:{20000 + secrets.randbelow(40000)}/callback"
    client_id = identity["clients"][CLI_CLIENT]["client_id"]

    def cli_token(role: str) -> str:
        return issue_id_token(cfg, identity, logins[role], client_id, loopback)

    other_token = _other_client_token(cfg, identity, routes, logins)
    if other_token is None:
        log.info("Skipping the other-client token case: no superset client secret or reporting route to use.")
    check_bearer_decisions(f"{routes[ref]}{API_PATH}", roles, lambda role: admits(ref, role), cli_token, other_token)
    log.info("Not checking an expired token: it needs a wait past the 5 minute ID token lifetime.")


def _other_client_token(
    cfg: E2EConfig, identity: Mapping[str, Any], routes: Mapping[str, str], logins: Mapping[str | None, str]
) -> Callable[[str], str] | None:
    """A token issued to superset, a confidential client whose secret is read from its Secret."""
    superset = identity.get("clients", {}).get("superset")
    reporting = [url for ref, url in routes.items() if ref.rsplit("/", 1)[-1] == "reporting"]
    if not superset or "secret_ref" not in superset or not reporting:
        return None
    ref = superset["secret_ref"]
    try:
        secret = k8s.secret_value(
            ref["name"], ref["key"].replace(".", "\\."), cfg.platform_namespace, kube_context=cfg.kube_context
        )
    except k8s.KubectlError:
        return None
    # Dex matches the registered redirect exactly: Superset's own callback (modules/identity/dex).
    redirect = f"{reporting[0].rstrip('/')}/oauth-authorized/olf"
    return lambda role: issue_id_token(cfg, identity, logins[role], superset["client_id"], redirect, secret)
