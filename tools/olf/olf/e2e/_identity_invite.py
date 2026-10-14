"""`olf users invite`, `resend` and `cancel` against the real issuer (#331).

The deployed realm has no mail server by default, so for this check only its
`smtpServer` is pointed at the throwaway mail sink and emptied again at the end.
A realm that already has SMTP is skipped: replacing `smtpServer` through the
admin API drops a stored password (see `_identity_mail`), so an operator's relay
is never touched. Message bodies are read here only to take the setup link, which
is followed and never logged.
"""

from __future__ import annotations

import contextlib
import html
import re
import secrets
from collections.abc import Callable
from typing import Any

import requests

from olf import log
from olf.e2e._identity_mail import action_link, sink_message_ids, sink_smtp, wait_for_delivery
from olf.e2e._identity_users import users_admin
from olf.e2e._shell import E2EConfig, E2EError
from olf.identity_admin import AdminError, DeliveryError

REQUEST_TIMEOUT = 15
ANCHOR = re.compile(r'<a[^>]+href="([^"]*action-token[^"]*)"')
FORM = re.compile(r'<form[^>]+id="([^"]+)"[^>]+action="([^"]+)"')
# Port 1 on Keycloak's own loopback refuses the connection at once.
CLOSED_PORT = {"host": "localhost", "port": "1"}


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise E2EError(message)


def _setup_session(link: str) -> tuple[requests.Session, str | None]:
    """Open a setup link as a browser would. The second value is the page's "proceed" target, or None when
    the link leads nowhere (a cancelled or expired invitation)."""
    browser = requests.Session()
    proceed = ANCHOR.search(browser.get(link, timeout=REQUEST_TIMEOUT).text)
    return browser, html.unescape(proceed[1]) if proceed else None


def _complete_setup(link: str, email: str, password: str) -> None:
    browser, proceed = _setup_session(link)
    _require(proceed is not None, "the setup link did not lead to account setup")
    page = browser.get(str(proceed), timeout=REQUEST_TIMEOUT)
    for _ in range(3):  # the password form, then the profile form Keycloak adds because the realm requires a name
        form = FORM.search(page.text)
        if form is None:
            return
        if form[1] == "kc-passwd-update-form":
            data = {"password-new": password, "password-confirm": password}
        else:
            data = {"email": email, "firstName": "OLF", "lastName": "E2E"}
        page = browser.post(html.unescape(form[2]), data=data, timeout=REQUEST_TIMEOUT)


def check_invitations(
    cfg: E2EConfig,
    identity: dict[str, Any],
    base_url: str,
    claims: Callable[[str, str], dict[str, Any]],
    master_admin: Callable[[], contextlib.AbstractContextManager[requests.Session]],
) -> None:
    """`claims(username, password)` returns the ID token's claims, or raises E2EError if login is refused;
    `master_admin()` opens a short-lived realm-management session."""
    realm_url = f"{base_url}/admin/realms/{identity['issuer_url'].rsplit('/', 1)[1]}"

    def put_smtp(smtp: dict[str, str]) -> None:
        with master_admin() as session:
            session.put(realm_url, json={"smtpServer": smtp}, timeout=REQUEST_TIMEOUT).raise_for_status()

    with master_admin() as session:
        if session.get(realm_url, timeout=REQUEST_TIMEOUT).json().get("smtpServer", {}).get("host"):
            log.info("Skipping the invitation checks: the realm has SMTP of its own, which a test must not replace.")
            return
    log.step("Checking olf users invite, resend and cancel against the test mail sink...")
    admin, _ = users_admin(cfg, identity, base_url, email_delivery=True)
    role, claim = identity["roles"]["precedence"][-1], identity["role_claim"]
    suffix = secrets.token_hex(3)
    member, withdrawn, stuck = (f"olf-e2e-{n}-{suffix}@example.invalid" for n in ("member", "withdrawn", "stuck"))

    def account(email: str) -> Any:
        return next((user for user in admin.users() if user.username == email), None)

    def refused(call: Callable[[], Any], match: str) -> None:
        try:
            call()
        except (AdminError, E2EError) as exc:
            _require(match in str(exc), f"expected {match!r}, got: {exc}")
        else:
            raise E2EError(f"expected {match!r}, but the call succeeded")

    try:
        refused(lambda: users_admin(cfg, identity, base_url)[0].invite(member, role), "cannot send mail")
        _require(account(member) is None, "an invitation the issuer could not mail created an account")

        put_smtp(sink_smtp(cfg))
        admin.invite(member, role)
        _require(
            (account(member).state, account(member).roles) == ("pending", (role,)), "an invited account is not pending"
        )
        refused(lambda: claims(member, "guessed"), "did not return an authorization code")
        wait_for_delivery(cfg, member)
        admin.resend(member)
        newest = action_link(cfg, wait_for_delivery(cfg, member, 2)[0])
        password = secrets.token_urlsafe(24)
        _complete_setup(newest, member, password)
        _require(account(member).state == "active", "the account is still pending after the member set a password")
        _require(claims(member, password).get(claim) == [role], "the new member's token lacks the invited role")
        refused(lambda: admin.resend(member), "is active, not pending")
        refused(lambda: admin.cancel(member), "is active, not pending")

        admin.invite(withdrawn, role)
        link = action_link(cfg, wait_for_delivery(cfg, withdrawn)[0])
        admin.cancel(withdrawn)
        _require(account(withdrawn) is None, "a cancelled invitation still has an account")
        _require(_setup_session(link)[1] is None, "a cancelled invitation's link still leads to account setup")
        refused(lambda: admin.resend(withdrawn), "No user")

        put_smtp(sink_smtp(cfg) | CLOSED_PORT)
        try:
            admin.invite(stuck, role)
        except DeliveryError:
            pass
        else:
            raise E2EError("an unreachable SMTP server was not reported as a delivery failure")
        _require(
            (account(stuck).state, account(stuck).roles) == ("pending", (role,)), "an undelivered invite lost its state"
        )
        _require(not sink_message_ids(cfg, stuck), "the sink holds mail for an invitation reported as undelivered")
        refused(lambda: claims(stuck, "guessed"), "did not return an authorization code")
        put_smtp(sink_smtp(cfg))
        admin.resend(stuck)
        wait_for_delivery(cfg, stuck)
        admin.cancel(stuck)
    finally:
        put_smtp({})
        with master_admin() as session:
            for email in (member, withdrawn, stuck):
                for found in session.get(f"{realm_url}/users", params={"username": email, "exact": "true"}).json():
                    session.delete(f"{realm_url}/users/{found['id']}", timeout=REQUEST_TIMEOUT)
            restored = session.get(realm_url, timeout=REQUEST_TIMEOUT).json()
            _require(not restored.get("smtpServer", {}).get("host"), "the realm's smtpServer was not emptied again")
