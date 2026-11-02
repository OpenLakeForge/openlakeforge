"""`olf infra source bind|unbind` -- write a stage's source bindings from `source.yaml` (#760)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from olf.common.log import fail
from olf.infra.deployment.source_bind import SourceBindingError, StageCluster

app = typer.Typer(help="Bind a source's runtime settings and credentials in a stage.")

_PROFILE = typer.Option(..., "--file", "-f", help="Deployment Profile v1 path.")
_STAGE = typer.Option("dev", "--stage", help="Enabled target stage: dev, uat, or prod.")


def _pairs(items: list[str], option: str) -> dict[str, str]:
    """KEY=VALUE pairs. A malformed item is never echoed: it may be a value."""
    pairs: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SourceBindingError(f"{option} expects KEY=VALUE")
        if key in pairs:
            raise SourceBindingError(f"{option} given twice for {key!r}")
        pairs[key] = value
    return pairs


def _secret_values(from_env: list[str], from_file: list[str]) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for key, var in _pairs(from_env, "--secret-from-env").items():
        if var not in os.environ:
            raise SourceBindingError(f"--secret-from-env {key}: environment variable {var} is not set")
        secrets[key] = os.environ[var]
    for key, path in _pairs(from_file, "--secret-from-file").items():
        if key in secrets:
            raise SourceBindingError(f"secret {key!r} given by both --secret-from-env and --secret-from-file")
        try:
            # A trailing newline is an editor's, not part of a token.
            secrets[key] = Path(path).read_text().removesuffix("\n")
        except OSError as exc:
            raise SourceBindingError(f"--secret-from-file {key}: cannot read {path}: {exc.strerror}") from exc
    return secrets


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _load(profile_file: str, stage: str, source_id: str):  # noqa: ANN202
    from openlakeforge_domain import load_lakehouse_inventory

    from olf.infra.commands._shared import deployment_context_for_profile
    from olf.infra.commands.project import _profile_provider

    context = deployment_context_for_profile(profile_file, stage=stage)
    sources = load_lakehouse_inventory(Path(profile_file).resolve().parent / "lakehouse_code").sources
    source = next((candidate for candidate in sources if candidate.name == source_id), None)
    if source is None:
        known = ", ".join(sorted(candidate.name for candidate in sources))
        raise SourceBindingError(f"unknown source {source_id!r}; this project has: {known}.")
    provider = _profile_provider(context)
    cluster = StageCluster(
        kubectl=provider.tools.kubectl,
        namespace=context.namespace,
        context=provider.env.get("KUBE_CONTEXT") or context.kube_context,
        kubeconfig=context.paths.kubeconfig_path,
        env=provider.env,
    )
    return source, cluster


@app.command("bind")
def bind(
    source_id: str = typer.Argument(..., metavar="SOURCE", help="Source id, as in its source.yaml."),
    profile_file: str = _PROFILE,
    stage: str = _STAGE,
    config: list[str] | None = typer.Option(None, "--set", help="KEY=VALUE for a `runtime.config` key. Repeatable."),
    secret_from_env: list[str] | None = typer.Option(
        None, "--secret-from-env", help="KEY=VAR: a `runtime.secrets` key read from environment variable VAR."
    ),
    secret_from_file: list[str] | None = typer.Option(
        None,
        "--secret-from-file",
        help="KEY=PATH: a `runtime.secrets` key read from a file, minus one trailing newline.",
    ),
    partial: bool = typer.Option(False, "--partial", help="Bind only the given keys and keep those already bound."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would change, by key name, and write nothing."),
) -> None:
    """Create or update the ConfigMap and Secret a source's `runtime` block declares.

    Keys are checked against `runtime.config` and `runtime.secrets`. Secret values
    never come from the command line; with none given for a declared secret, a
    terminal prompts for it without echo. Output names keys, never values.
    """
    from olf.infra.deployment.source_bind import bind as bind_source
    from olf.infra.deployment.source_bind import validate_keys

    try:
        source, cluster = _load(profile_file, stage, source_id)
        values, secrets = _pairs(config or [], "--set"), _secret_values(secret_from_env or [], secret_from_file or [])
        validate_keys(source, values, secrets, partial=True)
        if not (partial or dry_run) and _stdin_is_tty():
            for key in source.runtime_secrets:
                if key not in secrets:
                    secrets[key] = typer.prompt(f"{source.name} secret {key}", hide_input=True)
        lines = bind_source(source, values, secrets, cluster, partial=partial, dry_run=dry_run)
    except SourceBindingError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for line in lines:
        typer.echo(line)


@app.command("unbind")
def unbind(
    source_id: str = typer.Argument(..., metavar="SOURCE", help="Source id, as in its source.yaml."),
    profile_file: str = _PROFILE,
    stage: str = _STAGE,
) -> None:
    """Delete the ConfigMap and Secret that bind a source in a stage."""
    from olf.infra.deployment.source_bind import unbind as unbind_source

    try:
        source, cluster = _load(profile_file, stage, source_id)
        lines = unbind_source(source, cluster)
    except SourceBindingError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    for line in lines:
        typer.echo(line)
