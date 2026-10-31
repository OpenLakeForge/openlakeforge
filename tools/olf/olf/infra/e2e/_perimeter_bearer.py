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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from olf.common import log
from olf.infra import k8s
from olf.infra.e2e._identity_github import REQUEST_TIMEOUT, StubAuthorize, stub_host
from olf.infra.e2e._shell import E2EConfig, E2EError
from olf.platform.writes import zip_report

CLI_CLIENT = "olf-cli"
# Dagster has no authorization of its own, so a 2xx here is the perimeter's decision alone. `/graphql` is
# an API route (401, never a login redirect, without a session).
API_SERVICE = "orchestration"
API_PATH = "/graphql?query=%7Bversion%7D"
SCOPE = "openid email profile groups"
# The platform API answers under /api on the portal's host (ADR 0021).
PLATFORM_API_HOST_REF = "shared/portal"
# The logs and launch routes' rows in packages/platform-api's permissions.toml.
LOGS_FLOOR = "data-engineer"
# The read routes' rows: Dagster's at data-engineer on every stage, OpenMetadata's at viewer with no stage.
DAGSTER_READS_FLOOR = "data-engineer"
OPENMETADATA_READS_FLOOR = "viewer"
LAUNCH_FLOOR = "data-engineer"
LAUNCH_STAGES = ("dev",)
# No stage has a job by this name, so a launch request for it never starts a run.
ABSENT_JOB = "olf_e2e_no_such_job"
# The report import route's row: data-engineer and above, DEV only.
IMPORT_FLOOR = "data-engineer"
IMPORT_STAGES = ("dev",)


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
    scope: str = SCOPE,
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
            "scope": scope,
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
    issued: dict[str, str] = {}

    # One sign-in per role serves every check below; each is a full code flow at the stub.
    def cli_token(role: str) -> str:
        if role not in issued:
            issued[role] = issue_id_token(cfg, identity, logins[role], client_id, loopback)
        return issued[role]

    other_token = _other_client_token(cfg, identity, routes, logins)
    if other_token is None:
        log.info("Skipping the other-client token case: no superset client secret or reporting route to use.")
    check_bearer_decisions(f"{routes[ref]}{API_PATH}", roles, lambda role: admits(ref, role), cli_token, other_token)
    if PLATFORM_API_HOST_REF in routes:
        check_platform_api(
            routes[PLATFORM_API_HOST_REF].rstrip("/"),
            roles,
            cli_token,
            other_token,
            stage=cfg.namespace.removeprefix("olf-"),
            precedence=identity["roles"]["precedence"],
        )
        if cfg.inventory.dashboards:
            report_dir = (
                cfg.repo_root / min(cfg.inventory.dashboards, key=lambda d: d.report_source_dir).report_source_dir
            )
            check_report_import(
                routes[PLATFORM_API_HOST_REF].rstrip("/"),
                roles,
                cli_token,
                report_dir,
                stage=cfg.namespace.removeprefix("olf-"),
                precedence=identity["roles"]["precedence"],
            )
    log.info("Not checking an expired token: it needs a wait past the 5 minute ID token lifetime.")


def check_platform_api(
    base_url: str,
    roles: list[str],
    cli_token: Callable[[str], str],
    other_token: Callable[[str], str] | None,
    *,
    stage: str,
    precedence: Sequence[str],
) -> None:
    """Through the perimeter, /api/healthz answers, /api/v1/whoami names each mapped role the token carries, and
    the tokens the perimeter refuses are refused there too. Every canonical role clears the perimeter's lowest
    floor; the API's own matrix decides above it, as the stage's archived-logs and launch routes show."""
    log.step("Checking the platform API through the perimeter...")

    def get(path: str, role: str) -> requests.Response:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {cli_token(role)}"}
        return requests.get(f"{base_url}{path}", headers=headers, allow_redirects=False, timeout=REQUEST_TIMEOUT)

    health = get("/api/healthz", roles[0])
    if health.status_code != 200:
        raise E2EError(f"{base_url}/api/healthz through the perimeter: got HTTP {health.status_code}")
    for role in roles:
        reply = get("/api/v1/whoami", role)
        if reply.status_code != 200 or reply.json().get("role") != role:
            raise E2EError(f"{base_url}/api/v1/whoami for {role}: got HTTP {reply.status_code} {reply.text[:200]}")
    check_bearer_decisions(f"{base_url}/api/v1/whoami", roles, lambda role: True, cli_token, other_token)
    # A 2xx needs the API's read-only key to list the stage's log prefix: a key the
    # object store refuses is a 502, so this also proves the SeaweedFS grant. Today's
    # partition may still be empty, which is still a 200.
    logs = f"{base_url}/api/v1/stages/{stage}/logs?date={datetime.now(UTC).date().isoformat()}&limit=1"
    floor = precedence.index(LOGS_FLOOR)
    check_bearer_decisions(logs, roles, lambda role: precedence.index(role) <= floor, cli_token, other_token)
    check_platform_reads(base_url, roles, cli_token, other_token, stage=stage, precedence=precedence)
    check_launch_audit(base_url, roles, cli_token, stage=stage, precedence=precedence)


def check_platform_reads(
    base_url: str,
    roles: list[str],
    cli_token: Callable[[str], str],
    other_token: Callable[[str], str] | None,
    *,
    stage: str,
    precedence: Sequence[str],
) -> None:
    """The read routes `olf jobs`, `olf runs list|show`, `olf domain list` and `olf product list` call, each
    decided per role as its permissions.toml row says (#668). The suite has run the stage's pipelines by now,
    so a run exists to show; OpenMetadata answers with the caller's own token."""
    log.step("Checking the platform API's read routes for each role...")
    dagster = precedence.index(DAGSTER_READS_FLOOR)
    openmetadata = precedence.index(OPENMETADATA_READS_FLOOR)

    def reads_dagster(role: str) -> bool:
        return precedence.index(role) <= dagster

    def reads_openmetadata(role: str) -> bool:
        return precedence.index(role) <= openmetadata

    runs = f"{base_url}/api/v1/stages/{stage}/runs?limit=1"
    for url in (f"{base_url}/api/v1/stages/{stage}/jobs", runs):
        check_bearer_decisions(url, roles, reads_dagster, cli_token, other_token)
    reader = next(role for role in roles if reads_dagster(role))
    reply = requests.get(
        runs,
        headers={"Accept": "application/json", "Authorization": f"Bearer {cli_token(reader)}"},
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
    listed = reply.json().get("runs") if reply.status_code == 200 else None
    if not listed:
        raise E2EError(
            f"{runs} for {reader} listed no run after the suite ran the stage's pipelines: {reply.text[:200]}"
        )
    run = f"{base_url}/api/v1/stages/{stage}/runs/{listed[0]['run_id']}"
    check_bearer_decisions(run, roles, reads_dagster, cli_token, other_token)
    for path in ("/api/v1/domains", "/api/v1/data-products"):
        check_bearer_decisions(f"{base_url}{path}", roles, reads_openmetadata, cli_token, other_token)


def check_launch_audit(
    base_url: str, roles: list[str], cli_token: Callable[[str], str], *, stage: str, precedence: Sequence[str]
) -> None:
    """The first state-changing route, without changing anything: a launch of a job no stage has.

    The API stores the attempt with its write-only audit key before it looks the job up, and answers 503 without
    acting when it cannot. So a 404 at or above the floor proves the audit write path end to end, and nothing
    runs; below the floor, or on a stage outside the row, the answer is 403 (ADR 0021, decision 3).
    """
    url = f"{base_url}/api/v1/stages/{stage}/runs"
    floor = precedence.index(LAUNCH_FLOOR)
    for role in roles:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {cli_token(role)}"}
        reply = requests.post(
            url, headers=headers, json={"job": ABSENT_JOB}, allow_redirects=False, timeout=REQUEST_TIMEOUT
        )
        permitted = stage in LAUNCH_STAGES and precedence.index(role) <= floor
        expected = 404 if permitted else 403
        if reply.status_code != expected:
            hint = " (503: the audit record was not stored)" if reply.status_code == 503 else ""
            raise E2EError(
                f"{role} launching {ABSENT_JOB} at {url}: expected HTTP {expected}, got {reply.status_code}{hint}"
            )


def check_report_import(
    base_url: str,
    roles: list[str],
    cli_token: Callable[[str], str],
    report_dir: Path,
    *,
    stage: str,
    precedence: Sequence[str],
) -> None:
    """Each role imports the project's own report, zipped as `olf report import` zips it.

    At or above the floor on DEV the import is a 200: the API stored the attempt, signed in to Superset as its
    service user, found every schema the report reads already granted, and overwrote the assets the artifacts
    phase imported with the same content. Below the floor, or on another stage, it is a 403 and nothing is sent
    to Superset (ADR 0021, decision 3).
    """
    log.step(f"Checking report imports through the platform API with {report_dir.name}...")
    url = f"{base_url}/api/v1/stages/{stage}/reports"
    archive = zip_report(report_dir)
    floor = precedence.index(IMPORT_FLOOR)
    for role in roles:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {cli_token(role)}",
            "Content-Type": "application/zip",
        }
        reply = requests.post(url, headers=headers, data=archive, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        permitted = stage in IMPORT_STAGES and precedence.index(role) <= floor
        expected = 200 if permitted else 403
        if reply.status_code != expected:
            raise E2EError(
                f"{role} importing {report_dir.name} at {url}: expected HTTP {expected}, "
                f"got {reply.status_code} {reply.text[:300]}"
            )


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
