"""`olf login`, `olf whoami` and `olf logout`: the Platform CLI session (ADR 0020)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import typer

from olf.common.log import fail
from olf.platform import login as login_flow
from olf.platform import session as sessions

_PROFILE = typer.Option(
    "openlakeforge.yaml", "--profile-file", "-f", help="Deployment Profile of the deployment to sign in to."
)


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

    def sign_in(instance: sessions.Instance) -> dict[str, Any]:
        result = login_flow.login(instance, open_browser=not no_browser, announce=typer.echo)
        return sessions.claims(result.id_token)

    who = _run(profile_file, sign_in)
    typer.echo(f"Signed in as {who.get('preferred_username') or who.get('email', 'unknown')}.")


def whoami(profile_file: str = _PROFILE) -> None:
    """Show the signed-in identity, from the ID token claims."""

    def show(instance: sessions.Instance) -> None:
        who = sessions.claims(sessions.active_session(instance).id_token)
        expires = datetime.fromtimestamp(who.get("exp", 0), UTC).isoformat(timespec="seconds")
        typer.echo(f"Instance:  {instance.base_domain}")
        typer.echo(f"Issuer:    {instance.issuer}")
        typer.echo(f"Username:  {who.get('preferred_username', '-')}")
        typer.echo(f"Email:     {who.get('email', '-')}")
        typer.echo(f"Groups:    {', '.join(who.get('groups', [])) or '-'}")
        typer.echo(f"Expires:   {expires} (the ID token; it is refreshed on use)")

    _run(profile_file, show)


def logout(profile_file: str = _PROFILE) -> None:
    """Delete the stored session. The issuer's own session is not ended."""

    def end(instance: sessions.Instance) -> None:
        typer.echo(
            f"Signed out of {instance.base_domain}."
            if sessions.delete(instance)
            else f"Not logged in to {instance.base_domain}."
        )

    _run(profile_file, end)
