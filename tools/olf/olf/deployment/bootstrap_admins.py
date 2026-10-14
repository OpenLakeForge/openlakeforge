"""Invite the profile's `spec.identity.bootstrap_admins` as platform-admin at deploy (#331).

This runs after the issuer is ready and outside Terraform, so no user record enters
state. It only ever creates: an account that exists in any state (pending, active or
disabled) is left alone, so a redeploy cannot resend mail, reset a password, re-enable
an offboarded user or restore a revoked role, and removing an entry deletes nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from olf import identity_admin, log
from olf.deployment.errors import DeploymentError
from olf.identity_admin import AdminError, DeliveryError, KeycloakAdmin

ROLE = "platform-admin"


@dataclass
class Outcome:
    invited: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    unsent: dict[str, str] = field(default_factory=dict)  # email -> why no setup mail went out
    failed: dict[str, str] = field(default_factory=dict)  # email -> issuer error


def _invite(admin: KeycloakAdmin, email: str) -> None:
    try:
        admin.invite(email, ROLE)
    except AdminError:
        # A later deploy skips every existing account, so a pending one nobody was mailed would never be
        # retried. Taking it back keeps "no account" and "retry on the next deploy" the same state.
        if admin.has_account(email):
            with contextlib.suppress(AdminError):
                admin.cancel(email)
        raise


def invite_missing(admin: KeycloakAdmin, emails: Sequence[str]) -> Outcome:
    outcome = Outcome()
    for email in emails:
        try:
            if admin.has_account(email):
                outcome.existing.append(email)
            elif not admin.can_invite:
                outcome.unsent[email] = "no mail server is configured"
            else:
                _invite(admin, email)
                outcome.invited.append(email)
        except DeliveryError:
            outcome.unsent[email] = "the mail server did not take the message"
        except AdminError as exc:
            outcome.failed[email] = str(exc)
    return outcome


def onboard(identity: Mapping[str, Any], emails: Sequence[str], *, namespace: str, kube_context: str) -> None:
    """Invite the missing bootstrap administrators. Mail problems are reported and leave the deploy
    green: the platform is protected without them and `olf deploy` or `olf users invite` retries.
    A failure to reach or use the issuer fails the deploy, after every address was tried."""
    if not emails:
        return
    if not identity.get("capabilities", {}).get("admin_api"):
        log.warn(f"bootstrap_admins were not invited: {identity_admin.NO_ADMIN}")
        return
    log.step("Onboarding bootstrap administrators...")
    try:
        with identity_admin.connect(identity, secret_namespace=namespace, kube_context=kube_context) as admin:
            outcome = invite_missing(admin, emails)
    except AdminError as exc:
        raise DeploymentError(f"could not reach the issuer to onboard bootstrap administrators: {exc}") from exc
    if outcome.invited:
        log.info(f"Invited as {ROLE}, setup email sent: {', '.join(outcome.invited)}.")
    if outcome.existing:
        log.info(f"Already have an account, left unchanged: {', '.join(outcome.existing)}.")
    for email, why in outcome.unsent.items():
        log.warn(
            f"{email} was not invited: {why}. No account was created, so the platform stays protected. "
            f"Fix spec.identity.smtp and re-run `olf deploy`, or run `olf users invite {email} --role {ROLE}`."
        )
    if outcome.failed:
        detail = "; ".join(f"{email}: {why}" for email, why in outcome.failed.items())
        raise DeploymentError(f"bootstrap administrators were not all onboarded ({detail}). Re-run `olf deploy`.")
