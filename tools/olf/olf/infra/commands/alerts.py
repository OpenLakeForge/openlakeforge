"""`olf infra alerts bind|bindings` -- write and report alert receiver webhook URLs (#1425)."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from olf.common.log import fail
from olf.infra.commands.source import _stdin_is_tty
from olf.infra.deployment.source_bind import SourceBindingError, StageCluster

app = typer.Typer(help="Bind alert receivers' webhook URLs in the shared namespace.")

_PROFILE = typer.Option(..., "--file", "-f", help="Deployment Profile v1 path.")


def _context(profile_file: str):  # noqa: ANN202
    from olf.infra.commands._shared import deployment_context_for_profile

    return deployment_context_for_profile(profile_file)


def _cluster(context) -> StageCluster:  # noqa: ANN001
    from olf.infra.commands.project import _profile_provider

    provider = _profile_provider(context)
    return StageCluster(
        kubectl=provider.tools.kubectl,
        namespace=context.shared_namespace,
        context=provider.env.get("KUBE_CONTEXT") or context.kube_context,
        kubeconfig=context.paths.kubeconfig_path,
        env=provider.env,
    )


def _value(from_env: str | None, from_file: str | None, name: str) -> str:
    if from_env and from_file:
        raise SourceBindingError("give the URL with --from-env or --from-file, not both")
    if from_env:
        if from_env not in os.environ:
            raise SourceBindingError(f"--from-env: environment variable {from_env} is not set")
        return os.environ[from_env]
    if from_file:
        try:
            # A trailing newline is an editor's, not part of a URL.
            return Path(from_file).read_text().removesuffix("\n")
        except OSError as exc:
            raise SourceBindingError(f"--from-file: cannot read {from_file}: {exc.strerror}") from exc
    if _stdin_is_tty():
        return typer.prompt(f"receiver {name} webhook URL", hide_input=True)
    raise SourceBindingError(
        "no webhook URL: pass --from-env VAR or --from-file PATH, or run in a terminal to be asked"
    )


@app.command("bindings")
def bindings(profile_file: str = _PROFILE) -> None:
    """Report, per receiver, whether its Secret holds its key. Reads key names only."""
    from olf.infra.deployment.alert_bind import receiver_statuses

    context = _context(profile_file)
    receivers = context.topology.observability.alert_receivers
    if not receivers:
        typer.echo("No alert receivers are declared.")
        return
    cluster = _cluster(context)
    statuses = receiver_statuses(
        receivers,
        cluster.kubectl,
        namespace=cluster.namespace,
        context=cluster.context,
        kubeconfig=cluster.kubeconfig,
        env=cluster.env,
    )
    for status in statuses:
        where = f"secret {status.secret} key {status.key} in {cluster.namespace}"
        if status.present is None:
            typer.echo(f"receiver {status.receiver}: {where}: Secret missing")
        elif not status.bound:
            have = ", ".join(sorted(status.present)) or "(none)"
            typer.echo(f"receiver {status.receiver}: {where}: key missing (the Secret has: {have})")
        else:
            typer.echo(f"receiver {status.receiver}: {where}: bound")
    if not all(status.bound for status in statuses):
        raise typer.Exit(code=1)


@app.command("bind")
def bind(
    receiver_name: str = typer.Argument(..., metavar="RECEIVER", help="Receiver name, as in the profile."),
    profile_file: str = _PROFILE,
    from_env: str | None = typer.Option(
        None, "--from-env", help="Read the webhook URL from this environment variable."
    ),
    from_file: str | None = typer.Option(
        None, "--from-file", help="Read the webhook URL from a file, minus one trailing newline."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would change and write nothing."),
) -> None:
    """Write a receiver's webhook URL into the Secret its `secret_ref` names, in the shared namespace.

    The URL never comes from the command line; with neither option, a terminal
    prompts for it without echo. Other keys of a shared Secret are kept. It must
    use https. Output names the receiver, Secret and key, never the URL.
    """
    from olf.infra.deployment.alert_bind import bind_receiver, validate_url

    try:
        context = _context(profile_file)
        receivers = context.topology.observability.alert_receivers
        receiver = next((candidate for candidate in receivers if candidate.name == receiver_name), None)
        if receiver is None:
            known = ", ".join(sorted(candidate.name for candidate in receivers)) or "(none)"
            raise SourceBindingError(f"unknown alert receiver {receiver_name!r}; the profile declares: {known}.")
        value = _value(from_env, from_file, receiver.name)
        validate_url(value)
        cluster = _cluster(context)
        line = bind_receiver(receiver, value, cluster, dry_run=dry_run)
    except SourceBindingError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(line)
