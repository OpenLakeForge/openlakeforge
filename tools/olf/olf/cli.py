"""The olf CLI: entrypoint for OpenLakeForge deployment tooling.

Command groups live in `olf.commands.*`; this module only constructs the
root Typer application, registers each group, and wires `main()`.
"""

from __future__ import annotations

import json

import typer

import olf
from olf.commands import (
    access,
    artifacts,
    auth,
    catalog,
    checks,
    contracts,
    dbt,
    deployment,
    diagnostics,
    distribution,
    domain,
    e2e,
    floe,
    images,
    init,
    k8s,
    layers,
    openmetadata,
    platform,
    product,
    profile,
    project,
    release,
    smoke,
    source,
    superset,
    toolchain,
)

app = typer.Typer(
    name="olf",
    help="OpenLakeForge deployment tooling.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

app.add_typer(access.app, name="access")
app.add_typer(contracts.app, name="contracts")
app.add_typer(checks.app, name="check")
app.add_typer(dbt.app, name="dbt")
app.add_typer(diagnostics.app, name="diagnostics")
app.add_typer(distribution.app, name="distribution")
app.add_typer(images.app, name="images")
app.add_typer(catalog.app, name="catalog")
app.add_typer(floe.app, name="floe")
app.add_typer(artifacts.app, name="artifacts")
app.add_typer(auth.app, name="auth")
app.add_typer(layers.app, name="layers")
app.add_typer(superset.app, name="superset")
app.add_typer(superset.report_app, name="report")
app.add_typer(openmetadata.app, name="openmetadata")
app.add_typer(k8s.app, name="k8s")
app.add_typer(e2e.app, name="e2e")
app.add_typer(smoke.app, name="smoke")
app.add_typer(release.app, name="release")
app.add_typer(source.app, name="source")
app.add_typer(domain.app, name="domain")
app.add_typer(product.app, name="product")
app.add_typer(project.app, name="project")
app.add_typer(profile.app, name="profile")
app.add_typer(platform.app, name="platform")
app.add_typer(toolchain.app, name="toolchain")

app.command("deploy")(deployment.deploy)
app.command("plan")(deployment.plan)
app.command("doctor")(deployment.doctor)
app.command("destroy")(deployment.destroy)
app.command("status")(deployment.status)
app.command("forward")(deployment.forward)
app.command("init")(init.initialize)


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
    from olf.commands.distribution import version_payload
    from olf.distribution import DistributionError

    try:
        payload = json.loads(version_payload())
    except DistributionError as exc:
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"package_version": olf.__version__, **payload}, sort_keys=True))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
