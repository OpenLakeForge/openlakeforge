"""`olf infra backup`: a restorable copy of a deployment's data."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from olf.common.log import fail
from olf.infra.commands._shared import applied_runtime_environment, deployment_context, deployment_context_for_profile


def backup(
    output: str = typer.Option(..., "--output", help="New or empty directory to write the backup into."),
    profile_file: str = typer.Option(
        "", "--file", "-f", help="Deployment Profile v1 path the deployment was created from."
    ),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Back up the databases, object storage and data-bound Secrets of a deployed stack.

    Pauses the Dagster daemon of every stage and refuses while a run is in flight; the daemon is resumed
    whatever happens. The Secrets are sealed under a passphrase read from OLF_BACKUP_PASSPHRASE or a prompt, never
    from an argument. Keep the passphrase: without it the Secrets cannot be restored. Local provider only.
    """
    from openlakeforge_services.base import ServiceClientError

    import olf
    from olf.common import config, contract_state
    from olf.common.deployment.errors import DeploymentError
    from olf.common.provider_contracts import ProviderContractError, parse_provider_contracts
    from olf.infra import k8s
    from olf.infra.backup import BackupError
    from olf.infra.backup._backup import run_backup
    from olf.infra.backup._bundle import read_passphrase
    from olf.infra.backup._cluster import cluster_operations
    from olf.infra.backup._manifest import sha256_file
    from olf.infra.backup._plan import plan_from_contract

    if profile_file:
        context = deployment_context_for_profile(profile_file)
    else:
        context = deployment_context("", profile="", namespace="", cluster_name="", project_root=project_root)
    if context.provider.value != "local":
        raise typer.Exit(
            code=fail(f"backup supports the local provider only; this deployment is {context.provider.value}.")
        )
    profile_path = Path(profile_file) if profile_file else context.paths.repo_root / "openlakeforge.yaml"
    if not profile_path.is_file():
        raise typer.Exit(code=fail(f"{profile_path} not found; the backup records which profile it was taken with."))
    github = context.topology.identity.github if context.topology.identity else None
    try:
        # Asked before anything is paused: a prompt must not wait while the daemon is stopped.
        passphrase = read_passphrase(confirm=True, environ=os.environ)
        with applied_runtime_environment(context, env="local") as contract:
            raw = contract_state.load_provider_contracts(
                config.env("OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR", str(context.paths.platform_terraform_dir)),
                environ=context.command_env(base=os.environ),
            )
            if raw is None:
                raise BackupError("no provider contracts found; deploy the stack first.")
            contracts = parse_provider_contracts(raw, context.topology)
            plan = plan_from_contract(
                contracts,
                shared_namespace=context.shared_namespace,
                github_oauth_secret=github.oauth_app_secret_ref.name if github else None,
            )
            with cluster_operations(contracts, shared_namespace=context.shared_namespace, environ=contract) as ops:
                manifest = run_backup(
                    Path(output),
                    plan=plan,
                    stages={stage.name.value: stage.namespace for stage in contracts.stages.values()},
                    release=olf.__version__,
                    profile_sha256=sha256_file(profile_path),
                    provider=context.provider.value,
                    passphrase=passphrase,
                    ops=ops,
                )
    except (BackupError, DeploymentError, ProviderContractError, ServiceClientError, k8s.KubectlError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
    typer.echo(
        f"Backed up {len(manifest.databases)} databases, {len(manifest.buckets)} buckets and "
        f"{len(manifest.secrets)} Secrets to {output}. Keep the passphrase: the Secrets cannot be restored without it."
    )
