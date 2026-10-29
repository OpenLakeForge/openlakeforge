"""dbt artifact preparation without a shell environment wrapper."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import typer

from olf import config
from olf.commands._shared import fail
from olf.deployment.errors import DeploymentError
from olf.tooling.toolkit import Toolkit

app = typer.Typer(help="Render and validate dbt product projects.")


@app.command("parse")
def parse(
    project_dir: str = typer.Option("", "--project-dir", help="One dbt project; defaults to all products."),
    provider: str = typer.Option("local", "--provider", help="Provider owning the deployed contracts."),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Render profiles, resolve dependencies, and parse projects using provider contracts."""
    from olf.commands.runtime import provider_contract_environment

    try:
        with provider_contract_environment(
            provider=provider,
            profile=profile,
            namespace=namespace,
            cluster_name=cluster_name,
            kubeconfig_path=kubeconfig_path,
            project_root=project_root,
        ):
            # Resolved *inside* the contract environment: it's what sets
            # OPENLAKEFORGE_REPO_ROOT/OLF_DISTRIBUTION_ROOT from
            # --project-root in the first place, so reading these before
            # entering it always resolved the ambient/default roots
            # instead. `libs` is distribution-owned, not project-owned -
            # a --project-root selection containing only lakehouse_code
            # (per the image-build contract) has no `libs` of its own, so
            # the distribution root must be on sys.path too, not just the
            # project root.
            project_spec = config.project_spec()
            for path in (str(project_spec.root), str(project_spec.distribution_root)):
                if path not in sys.path:
                    sys.path.insert(0, path)
            from libs.dbt.render_profiles import discover_project_dirs, write_profile

            projects = (
                [Path(project_dir).resolve()] if project_dir else discover_project_dirs(project_spec.gold_root)
            )
            if not projects:
                raise typer.Exit(code=fail("no product dbt projects found"))
            tools = Toolkit.default()
            dbt = str(tools.resolver.resolve("dbt"))
            for project in projects:
                # Outside the project: a profiles.yml left in it is a stage
                # binding sitting next to the frozen dbt sources (#546).
                with tempfile.TemporaryDirectory(prefix="olf-dbt-profiles-") as profiles_dir:
                    write_profile(project, output_dir=Path(profiles_dir))
                    for command in ("deps", "parse"):
                        args = [dbt, command, "--project-dir", str(project)]
                        if command == "parse":
                            args += ["--profiles-dir", profiles_dir]
                        tools.runner.run(args, cwd=project_spec.root, stream_output=True)
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc

