"""`olf toolchain`: inspect and manage the versioned managed toolchain (#127).

Terraform/Helm/kubectl/kind are provisioned from `release/component-catalog.yaml`
into `OLF_HOME` (default `~/.openlakeforge`), scoped per distribution version
and platform. See `olf.toolchain` for the manager, and
`olf.tooling.resolver.build_resolver` for how deployment commands pick this
up automatically.
"""

from __future__ import annotations

import typer

from olf.log import fail

app = typer.Typer(help="Inspect and manage the managed CLI toolchain (Terraform, Helm, kubectl, kind).")


def _manager():  # noqa: ANN202
    """Resolve the catalog through the active runtime layout.

    `olf toolchain` is a top-level command, so it runs *outside* the scoped
    environment `DeploymentContext.command_env` builds - it cannot read
    `OLF_DISTRIBUTION_ROOT` from a parent `olf deploy`. Resolving the
    catalog from the current directory (the previous behaviour) meant an
    installed user running `olf toolchain install` from their own project
    folder got "release catalog not found at ./release/component-catalog.yaml"
    instead of the payload's catalog. `runtime_layout()` returns the right
    catalog in both modes, and still honours an explicit
    `OLF_DISTRIBUTION_ROOT`/`OPENLAKEFORGE_REPO_ROOT` override.
    """
    from olf.distribution import runtime_layout
    from olf.toolchain.manager import ToolchainManager

    return ToolchainManager.from_catalog_path(runtime_layout().catalog_path)


@app.command("list")
def list_tools() -> None:
    """Show each managed tool's declared (catalog) and installed versions."""
    from olf.distribution import DistributionError
    from olf.toolchain.errors import ToolchainError

    try:
        manager = _manager()
    except (ToolchainError, DistributionError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc

    typer.echo(f"distribution {manager.distribution_version}  platform {manager.platform}  home {manager.home}")
    for name, spec in manager.specs.items():
        installed = manager.installed(name)
        if installed is None:
            state = "not installed"
        elif installed.version == spec.version and installed.sha256 == spec.sha256:
            state = f"installed at {installed.path}"
        else:
            state = f"stale (installed {installed.version}, catalog wants {spec.version}) at {installed.path}"
        typer.echo(f"  {name:10} catalog={spec.version:12} {state}")


@app.command("install")
def install_tools(
    tool: str = typer.Option("", "--tool", help="Provision only this tool; default provisions all managed tools."),
) -> None:
    """Provision managed tools from the release catalog (CI calls this to pre-warm the cache)."""
    try:
        manager = _manager()
        if tool:
            if tool not in manager.specs:
                raise typer.Exit(code=fail(f"unknown --tool: {tool!r} (expected one of {tuple(manager.specs)})"))
            path = manager.resolve(tool)
            typer.echo(f"{tool} -> {path}")
            return
        for name, path in manager.ensure_all().items():
            typer.echo(f"{name} -> {path}")
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - this command calls ToolchainManager
        # directly, bypassing ManagedExecutableResolver's catch-and-wrap, so
        # provisioning failures here can be anything the manager's own I/O
        # raises natively (PermissionError, a full disk, a corrupt archive
        # extraction) - all of it funnels into one concise CLI failure
        # rather than a raw traceback.
        raise typer.Exit(code=fail(str(exc))) from exc


@app.command("path")
def path(
    tool: str = typer.Argument("", help="Print only this tool's resolved path; default prints the managed bin dir."),
) -> None:
    """Print the managed bin directory, or one tool's resolved (and provisioned) path."""
    try:
        manager = _manager()
        if not tool:
            typer.echo(str(manager.bin_dir))
            return
        if tool not in manager.specs:
            raise typer.Exit(code=fail(f"unknown tool: {tool!r} (expected one of {tuple(manager.specs)})"))
        typer.echo(str(manager.resolve(tool)))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - see install_tools
        raise typer.Exit(code=fail(str(exc))) from exc


@app.command("clean")
def clean(
    version: str = typer.Option("", "--version", help="Remove only this distribution version's toolchain."),
    keep_current: bool = typer.Option(
        False, "--keep-current", help="Remove every installed version except the current distribution's."
    ),
    all_versions: bool = typer.Option(False, "--all", help="Remove every installed toolchain version."),
) -> None:
    """Remove installed toolchain versions under OLF_HOME. Never touches host-installed tools."""
    selected = sum(bool(x) for x in (version, keep_current, all_versions))
    if selected != 1:
        raise typer.Exit(code=fail("exactly one of --version, --keep-current, or --all is required"))
    try:
        manager = _manager()
        removed = manager.prune(
            version=version or None,
            keep_current=keep_current,
            remove_all=all_versions,
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - see install_tools
        raise typer.Exit(code=fail(str(exc))) from exc
    if not removed:
        typer.echo("Nothing to remove.")
        return
    for entry in removed:
        typer.echo(f"removed {entry}")
