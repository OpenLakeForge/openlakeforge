"""`olf infra restore`: put a backup `olf infra backup` took back onto a deployed platform of the same release."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from olf.common import log
from olf.common.log import fail
from olf.infra.commands._shared import applied_runtime_environment
from olf.infra.commands._shared import deployment_context as _build_context
from olf.infra.commands.deployment import _build_engine


def restore(
    source: str = typer.Option(..., "--from", help="Backup directory `olf infra backup` wrote."),
    project_root: str = typer.Option(
        "", "--project-root", help="Writable project root; defaults to the current directory."
    ),
) -> None:
    """Restore databases, objects and Secrets from a backup, then reconcile the platform and reactivate its revisions.

    The platform must already be deployed without the project (`olf infra platform apply`, which also creates the
    cluster; `olf infra deploy --phase platform` when it still exists) from the release and the
    openlakeforge.yaml the backup was taken with. It pauses the Dagster daemons and refuses while a run is in flight.
    Each bucket is made equal to the backup's: objects written after it are deleted. The passphrase comes from a
    prompt or the environment variable `olf infra backup` documents. If a step fails, the workloads stay scaled to 0
    and the error says how to retry. Each stage gets back the project revision its restored activation pointer
    names, whatever the current checkout holds; a stage with no pointer is left alone. A revision whose image no
    longer exists (built with `olf infra project image --no-push`) fails the restore and names it. A backup that
    holds `local-ca` also has the CA republished and every Certificate it issued re-signed, and the workloads that
    mount them restarted, after the revisions are back.
    """
    import olf
    from olf.common import config
    from olf.common.deployment.errors import DeploymentError
    from olf.infra import k8s
    from olf.infra.backup import BackupError
    from olf.infra.backup._bundle import read_passphrase
    from olf.infra.backup._cluster import dagster_runs_in_flight
    from olf.infra.backup._restore import reissue_after_reconcile
    from olf.infra.backup._restore import restore as restore_backup
    from olf.infra.deployment.engine import DeploymentPhase
    from olf.infra.deployment.revision_build import reactivate_active_revision

    context = _build_context(
        "", profile="", namespace="", cluster_name="", project_root=project_root, github_connect=False
    )
    if context.provider.value != "local":
        raise typer.Exit(
            code=fail(f"the profile targets provider {context.provider.value!r}; only local is supported.")
        )
    try:
        passphrase = read_passphrase(confirm=False, environ=os.environ)
        with applied_runtime_environment(context, env="local") as contract:
            log_prefix = config.env("OPENLAKEFORGE_PORT_FORWARD_LOG_PREFIX", "/tmp/openlakeforge", environ=contract)
            ca_restored = restore_backup(
                Path(source),
                passphrase=passphrase,
                release=olf.__version__,
                profile=context.paths.repo_root / "openlakeforge.yaml",
                stage_namespaces={stage.value: context.namespace_for(stage) for stage in context.enabled_stages},
                shared_namespace=context.shared_namespace,
                runs_in_flight=lambda namespace: dagster_runs_in_flight(
                    namespace, log_path=f"{log_prefix}-restore-dagster.log", environ=contract
                ),
                environ=contract,
            )
        # Not ALL: the artifacts phase builds a revision from the checkout and activates it, which would replace
        # the restored one with whatever the checkout holds now.
        _build_engine(context, var_file="").deploy(DeploymentPhase.PLATFORM)
        for stage in context.enabled_stages:
            stage_context = _build_context(
                "", profile="", namespace="", cluster_name="", project_root=project_root, stage=stage.value
            )
            provider = _build_engine(stage_context, var_file="").provider
            if reactivate_active_revision(provider) is None:
                log.info(f"Stage {stage.value} has no active project revision; left as restored.")
        # Last, so the restarts also pick up the workloads the reactivation rolled out.
        if ca_restored:
            with applied_runtime_environment(context, env="local") as contract:
                reissue_after_reconcile(
                    {stage.value: context.namespace_for(stage) for stage in context.enabled_stages},
                    context.shared_namespace,
                    contract,
                )
    except (BackupError, DeploymentError, k8s.KubectlError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
