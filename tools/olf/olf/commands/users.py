"""User and role administration through the issuer's admin API (ADR 0014 seam 4, #331)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Annotated, Any

import typer

from olf import identity_admin
from olf.commands._shared import deployment_context, fail

app = typer.Typer(help="Users and their canonical roles, administered in the identity issuer.")

_Provider = Annotated[str, typer.Option("--provider", help="Deployment provider whose issuer to administer.")]
_AsJson = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
_User = Annotated[str, typer.Argument(help="Username or email.")]
_Email = Annotated[str, typer.Argument(help="The member's email address; it is also their username.")]
_Role =Annotated[str, typer.Option("--role", help="Canonical role; see `olf users roles`.")]


def _identity(provider: str) -> tuple[dict[str, Any], str, str]:
    """The applied contract's identity binding, the shared namespace and the kube context."""
    from olf import contracts
    from olf.deployment.errors import DeploymentError
    from olf.provider_contracts import ProviderContractError

    context = deployment_context(provider, profile="", namespace="", cluster_name="")
    environ = context.command_env(base=os.environ)
    try:
        payload = contracts.load_provider_contracts(
            str(context.paths.platform_terraform_dir), environ=environ, honor_contract_file=False, strict_state=True
        )
    except (DeploymentError, ProviderContractError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    if payload is None:
        raise typer.Exit(code=fail("No applied provider contract: run `olf deploy` first."))
    os.environ["KUBECONFIG"] = environ["KUBECONFIG"]
    return payload["shared"]["identity"], context.shared_namespace, context.kube_context


def _run(provider: str, action: Callable[[identity_admin.KeycloakAdmin], Any], as_json: bool = False) -> Any:
    identity, namespace, kube_context = _identity(provider)
    try:
        with identity_admin.connect(identity, secret_namespace=namespace, kube_context=kube_context) as admin:
            return action(admin)
    except identity_admin.DeliveryError as exc:
        # Exit 2, not 1: the issuer works and the account exists; only the mail did not go out.
        document = {"user": exc.email, "state": "pending", "email_sent": False, "error": str(exc)}
        typer.echo(json.dumps(document, indent=2) if as_json else str(exc), err=not as_json)
        raise typer.Exit(code=2) from exc
    except identity_admin.AdminError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc


def _emit(as_json: bool, document: dict[str, Any], text: str) -> None:
    typer.echo(json.dumps(document, indent=2) if as_json else text)


@app.command("roles")
def roles(provider: _Provider = "local", as_json: _AsJson = False) -> None:
    """The canonical roles, highest first, and the issuer group each maps to."""
    identity, _, _ = _identity(provider)
    model = identity_admin.RoleModel.from_contract(identity)
    rows = [{"role": role, "groups": list(model.groups.get(role, ()))} for role in model.precedence]
    text = "\n".join(f"{row['role']:<16}{', '.join(row['groups'])}" for row in rows)
    _emit(as_json, {"claim": identity["role_claim"], "roles": rows}, f"Role claim: {identity['role_claim']}\n{text}")


@app.command("list")
def list_users(provider: _Provider = "local", as_json: _AsJson = False) -> None:
    """Users with their canonical roles and state; a user with no recognised role is denied everywhere."""
    users = _run(provider, lambda admin: admin.users())
    unrecognised = [user.username for user in users if not user.roles]
    lines = [f"{u.username:<28}{u.email or '-':<32}{u.state:<10}{', '.join(u.roles) or 'NO ROLE'}" for u in users]
    if unrecognised:
        lines.append(f"{len(unrecognised)} user(s) hold no recognised role: grant one with `olf users grant`.")
    document = {"users": [u.as_json() for u in users], "unrecognised": unrecognised}
    _emit(as_json, document, "\n".join(lines) or "No users.")


@app.command("grant")
def grant(user: _User, role: _Role, provider: _Provider = "local", as_json: _AsJson = False) -> None:
    """Add a canonical role. Holding several gives the highest one's reach."""
    _run(provider, lambda admin: admin.grant(user, role))
    _emit(as_json, {"user": user, "granted": role}, f"Granted {role} to {user}.")


@app.command("revoke")
def revoke(user: _User, role: _Role, provider: _Provider = "local", as_json: _AsJson = False) -> None:
    """Remove a canonical role."""
    _run(provider, lambda admin: admin.revoke(user, role))
    _emit(as_json, {"user": user, "revoked": role}, f"Revoked {role} from {user}.")


@app.command("invite")
def invite(email: _Email, role: _Role, provider: _Provider = "local", as_json: _AsJson = False) -> None:
    """Create a pending account with a role and email it a setup link; the member chooses their own password."""
    _run(provider, lambda admin: admin.invite(email, role), as_json)
    hours = identity_admin.SETUP_LIFESPAN // 3600
    text = f"Invited {email} as {role}: a setup email was sent, valid for {hours} hours."
    _emit(as_json, {"user": email, "state": "pending", "role": role, "email_sent": True}, text)


@app.command("resend")
def resend(email: _Email, provider: _Provider = "local", as_json: _AsJson = False) -> None:
    """Email a pending account a fresh setup link; accounts that finished setup or are disabled are refused."""
    _run(provider, lambda admin: admin.resend(email), as_json)
    _emit(as_json, {"user": email, "state": "pending", "email_sent": True}, f"A new setup email was sent to {email}.")


@app.command("cancel")
def cancel(email: _Email, provider: _Provider = "local", as_json: _AsJson = False) -> None:
    """Remove a pending account so its setup links stop working."""
    _run(provider, lambda admin: admin.cancel(email))
    _emit(as_json, {"user": email, "cancelled": True}, f"Cancelled the invitation of {email}.")


def _toggle(enabled: bool) -> Callable[..., None]:
    def command(user: _User, provider: _Provider = "local", as_json: _AsJson = False) -> None:
        _run(provider, lambda admin: admin.set_enabled(user, enabled))
        state = "enabled" if enabled else "disabled"
        _emit(as_json, {"user": user, "state": state}, f"{user} is {state}.")

    return command


app.command("disable", help="Disable an account and end its issuer sessions.")(_toggle(False))
app.command("enable", help="Enable a disabled account.")(_toggle(True))
