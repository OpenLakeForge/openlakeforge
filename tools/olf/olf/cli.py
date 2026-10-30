"""The olf CLI: entrypoint for OpenLakeForge deployment tooling.

Command groups live in `olf.common.commands.*`; this module only constructs the
root Typer application, registers each group, and wires `main()`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable

import typer
from typer.models import CommandInfo, TyperInfo

import olf
from olf.common.commands import (
    checks,
    contracts,
    diagnostics,
    distribution,
    domain,
    images,
    init,
    layers,
    product,
    profile,
    project,
    release,
    source,
    toolchain,
)
from olf.infra.commands import (
    access,
    artifacts,
    auth,
    break_glass,
    catalog,
    dbt,
    deployment,
    e2e,
    github,
    k8s,
    openmetadata,
    platform,
    query,
    secrets,
    smoke,
    superset,
)
from olf.infra.commands import contracts as infra_contracts
from olf.infra.commands import diagnostics as infra_diagnostics
from olf.infra.commands import images as infra_images
from olf.infra.commands import product as infra_product
from olf.infra.commands import project as infra_project
from olf.platform import commands as platform_commands

app = typer.Typer(
    name="olf",
    help="OpenLakeForge deployment tooling.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
infra_app = typer.Typer(
    help="Commands that need cluster or cloud permissions.",
    no_args_is_help=True,
)


def _hidden[T: (CommandInfo, TyperInfo)](info: T) -> T:
    alias = copy.copy(info)
    alias.hidden = True
    return alias


def _infra_group(group: typer.Typer, name: str, *, common: typer.Typer | None = None) -> None:
    """Register an infra group under `olf infra` and at its pre-2b top-level path.

    The old path is a hidden alias of the same Typer app. When `common` also owns
    commands in that group (`project validate`), the old path is the common
    group with the infra commands added hidden, since Typer cannot merge two
    apps registered under one name.
    """
    infra_app.add_typer(group, name=name)
    if common is None:
        app.add_typer(group, name=name, hidden=True)
        return
    legacy = typer.Typer(help=common.info.help)
    legacy.registered_commands = [*common.registered_commands, *map(_hidden, group.registered_commands)]
    legacy.registered_groups = [*common.registered_groups, *map(_hidden, group.registered_groups)]
    app.add_typer(legacy, name=name)


def _infra_command(name: str, callback: Callable[..., None]) -> None:
    infra_app.command(name)(callback)
    app.command(name, hidden=True)(callback)


app.add_typer(infra_app, name="infra")

app.add_typer(checks.app, name="check")
app.add_typer(distribution.app, name="distribution")
app.add_typer(layers.app, name="layers")
app.add_typer(release.app, name="release")
app.add_typer(source.app, name="source")
app.add_typer(domain.app, name="domain")
app.add_typer(profile.app, name="profile")
app.add_typer(toolchain.app, name="toolchain")

_infra_group(access.app, "access")
_infra_group(github.app, "github")
_infra_group(secrets.app, "secrets")
_infra_group(infra_contracts.app, "contracts", common=contracts.app)
_infra_group(dbt.app, "dbt")
_infra_group(infra_diagnostics.app, "diagnostics", common=diagnostics.app)
_infra_group(infra_images.app, "images", common=images.app)
_infra_group(catalog.app, "catalog")
_infra_group(artifacts.app, "artifacts")
_infra_group(auth.app, "auth")
_infra_group(superset.app, "superset")
_infra_group(superset.report_app, "report")
_infra_group(openmetadata.app, "openmetadata")
_infra_group(k8s.app, "k8s")
_infra_group(e2e.app, "e2e")
_infra_group(smoke.app, "smoke")
_infra_group(infra_product.app, "product", common=product.app)
_infra_group(infra_project.app, "project", common=project.app)
_infra_group(platform.app, "platform")

_infra_command("deploy", deployment.deploy)
_infra_command("plan", deployment.plan)
_infra_command("doctor", deployment.doctor)
_infra_command("destroy", deployment.destroy)
_infra_command("status", deployment.status)
_infra_command("forward", deployment.forward)
_infra_command("break-glass", break_glass.break_glass_command)
_infra_command("query", query.query)
app.command("init")(init.initialize)
app.command("login")(platform_commands.login)


@app.callback()
def _root() -> None:
    """OpenLakeForge deployment tooling."""


@app.command()
def version(
    json_output: bool = typer.Option(False, "--json", help="Render package and payload identity as JSON."),
) -> None:
    """Print the tooling and active distribution version."""
    if not json_output:
        typer.echo(olf.__version__)
        return
    from olf.common.commands.distribution import version_payload
    from olf.common.distribution import DistributionError

    try:
        payload = json.loads(version_payload())
    except DistributionError as exc:
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"package_version": olf.__version__, **payload}, sort_keys=True))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
