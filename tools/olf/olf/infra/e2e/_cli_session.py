"""The Platform CLI's own session (#536): `olf login`, `olf whoami` and `olf logout` against the deployment's Dex.

The checks call the functions those commands are made of: `login.login` runs the
Authorization Code + PKCE flow with its own loopback listener, `session.active_session`
is what `olf whoami` reads, and `session.delete` is what `olf logout` does. Only the
Typer wrapper and the Deployment Profile lookup are left out: the instance comes from
the applied identity contract, which the profile rendered. The stub GitHub user signs
in where a person would use a browser, and the redirect reaches the CLI's listener for
real. Sessions are written under a temporary OLF_HOME, never the operator's.
"""

from __future__ import annotations

import os
import queue
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import requests

from olf.common import log
from olf.infra.e2e._identity_github import REQUEST_TIMEOUT, StubAuthorize, stub_host
from olf.infra.e2e._shell import E2EConfig, E2EError
from olf.platform import login as login_flow
from olf.platform import session as sessions

CLI_CLIENT = "olf-cli"
# Long enough for Dex and the stub; the CLI's own wait is five minutes.
SIGN_IN_SECONDS = 120


@contextmanager
def _olf_home() -> Iterator[None]:
    previous = os.environ.get("OLF_HOME")
    with tempfile.TemporaryDirectory(prefix="olf-e2e-home-") as home:
        os.environ["OLF_HOME"] = home
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("OLF_HOME", None)
            else:
                os.environ["OLF_HOME"] = previous


def cli_instance(identity: Mapping[str, Any]) -> sessions.Instance:
    """The instance `olf login -f <profile>` resolves on a Dex deployment: issuer `auth.<base_domain>`."""
    issuer = str(identity["issuer_url"]).rstrip("/")
    host = urlsplit(issuer).hostname or ""
    client_id = identity["clients"][CLI_CLIENT]["client_id"]
    return sessions.Instance(host.removeprefix("auth."), issuer, client_id, dex=True)


def check_cli_session(
    cfg: E2EConfig, identity: Mapping[str, Any], role: str, login: str, whoami_url: str | None
) -> None:
    """Sign in as `login` with the CLI's own flow, show who is signed in, sign out, and show nobody is."""
    log.step(f"Checking olf login, whoami and logout as {role}...")
    instance = cli_instance(identity)
    with _olf_home():
        session = _sign_in(cfg, instance, login)
        who = sessions.claims(sessions.active_session(instance).id_token)
        if who.get("email") != sessions.claims(session.id_token).get("email") or not who.get("groups"):
            raise E2EError(f"olf whoami after olf login as {login} shows {who.get('email')} in {who.get('groups')}")
        if whoami_url:
            # The stored session is the one every platform command sends.
            token = sessions.bearer_token(instance)
            reply = requests.get(
                whoami_url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT,
            )
            if reply.status_code != 200 or reply.json().get("role") != role:
                raise E2EError(f"{whoami_url} with the stored session: HTTP {reply.status_code} {reply.text[:200]}")
        if not sessions.delete(instance):
            raise E2EError("olf logout found no session to delete after olf login")
        try:
            sessions.active_session(instance)
        except sessions.PlatformError as exc:
            if "olf login" not in str(exc):
                raise E2EError(f"olf whoami after olf logout: {exc}") from exc
        else:
            raise E2EError("olf whoami after olf logout still shows a session")


def _sign_in(cfg: E2EConfig, instance: sessions.Instance, login: str) -> sessions.Session:
    announced: queue.Queue[str] = queue.Queue()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["session"] = login_flow.login(instance, open_browser=False, announce=announced.put)
        except Exception as exc:  # noqa: BLE001 - reported in the caller's thread
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        message = announced.get(timeout=SIGN_IN_SECONDS)
    except queue.Empty as exc:
        raise E2EError(f"olf login printed no sign-in URL: {outcome.get('error')}") from exc
    url = message.splitlines()[-1].strip()
    browser = requests.Session()
    browser.mount(f"https://{stub_host(cfg)}", StubAuthorize(login))
    # The redirect ends at the CLI's loopback listener, which answers it and hands the code to the flow.
    landed = browser.get(url, timeout=REQUEST_TIMEOUT)
    worker.join(SIGN_IN_SECONDS)
    if worker.is_alive():
        raise E2EError(f"olf login did not finish after the sign-in ended at {urlsplit(landed.url).path}")
    if "error" in outcome:
        raise E2EError(f"olf login as {login}: {outcome['error']}")
    session: sessions.Session = outcome["session"]
    return session
