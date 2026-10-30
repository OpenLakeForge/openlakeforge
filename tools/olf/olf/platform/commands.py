"""`olf login`: the Platform CLI session (ADR 0020)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import typer

from olf.common.log import fail
from olf.platform import login as login_flow
from olf.platform import session as sessions

_PROFILE = typer.Option(
    "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile of the deployment to sign in to."
)


ANONYMOUS = "Sign-in is disabled on this deployment; platform commands run without login."


def _run[T](profile_file: str, action: Callable[[sessions.Instance], T]) -> T:
    try:
        return action(sessions.resolve_instance(profile_file))
    except sessions.PlatformError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def login(
    profile_file: str = _PROFILE,
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the sign-in URL instead of opening it."),
) -> None:
    """Sign in to the deployment named by the Deployment Profile.

    A local deployment serves the issuer with its own CA: run `olf infra access
    trust` and set REQUESTS_CA_BUNDLE (or SSL_CERT_FILE) to the certificate it writes.
    """

    def sign_in(instance: sessions.Instance) -> dict[str, Any] | None:
        if instance.anonymous:
            return None
        result = login_flow.login(instance, open_browser=not no_browser, announce=typer.echo)
        return sessions.claims(result.id_token)

    who = _run(profile_file, sign_in)
    if who is None:
        typer.echo(ANONYMOUS)
        return
    typer.echo(f"Signed in as {who.get('preferred_username') or who.get('email', 'unknown')}.")
