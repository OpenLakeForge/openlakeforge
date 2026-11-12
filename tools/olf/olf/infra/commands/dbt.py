"""dbt artifact preparation without a shell environment wrapper."""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from olf.common import config
from olf.common.deployment.errors import DeploymentError
from olf.common.log import fail
from olf.common.tooling.process import unsets_of
from olf.common.tooling.toolkit import Toolkit
from olf.infra.commands._shared import PROVIDER_HELP

app = typer.Typer(help="Render and validate the Gold dbt project.")


@app.command("parse")
def parse(
    project_dir: str = typer.Option("", "--project-dir", help="The dbt project; defaults to lakehouse_code/gold."),
    provider: str = typer.Option("", "--provider", help=PROVIDER_HELP),
    profile: str = typer.Option("", "--profile", help="Deprecated single-DEV preset shorthand: 'full' or 'slim'."),
    namespace: str = typer.Option("", "--namespace", help="Kubernetes namespace override."),
    cluster_name: str = typer.Option("", "--cluster-name", help="Local kind cluster name override."),
    kubeconfig_path: str = typer.Option("", "--kubeconfig-path", help="Kubeconfig file path override."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Render the profile, resolve dependencies, and parse the project using provider contracts."""
    from olf.infra.commands.runtime import provider_contract_environment

    try:
        with provider_contract_environment(
            provider=provider,
            profile=profile,
            namespace=namespace,
            cluster_name=cluster_name,
            kubeconfig_path=kubeconfig_path,
            project_root=project_root,
        ) as contract:
            # Resolved *inside* the contract environment: it's what sets
            # OPENLAKEFORGE_REPO_ROOT/OLF_DISTRIBUTION_ROOT from
            # --project-root in the first place, so reading these before
            # entering it always resolved the ambient/default roots
            # instead.
            project_spec = config.project_spec(environ=contract)
            from openlakeforge_runtime.dbt.render_profiles import dbt_environment, gold_project_dir, write_profile

            project = Path(project_dir).resolve() if project_dir else gold_project_dir(project_spec.gold_root)
            if project is None:
                raise typer.Exit(code=fail(f"no Gold dbt project at {project_spec.gold_root / 'dbt_project.yml'}"))
            tools = Toolkit.default(environ=contract)
            dbt = str(tools.resolver.resolve("dbt"))
            # Outside the project: a profiles.yml left in it is a stage
            # binding sitting next to the frozen dbt sources (#546).
            with tempfile.TemporaryDirectory(prefix="olf-dbt-profiles-") as profiles_dir:
                write_profile(project, output_dir=Path(profiles_dir))
                for command in ("deps", "parse"):
                    args = [dbt, command, "--project-dir", str(project)]
                    if command == "parse":
                        args += ["--profiles-dir", profiles_dir]
                    tools.runner.run(
                        args,
                        cwd=project_spec.root,
                        env={**contract, **dbt_environment()},
                        stream_output=True,
                        unset=unsets_of(contract),
                    )
    except DeploymentError as exc:
        raise typer.Exit(code=fail(str(exc))) from exc

