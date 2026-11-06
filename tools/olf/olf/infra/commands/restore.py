"""`olf infra restore`: put a backup `olf infra backup` took back onto a deployed platform of the same release."""

from __future__ import annotations

import os
from pathlib import Path

import typer

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
    """Restore databases, objects and Secrets from a backup, then run `olf infra deploy` to reconcile.

    The platform must already be deployed (`olf infra deploy --phase platform`) from the release and the
    openlakeforge.yaml the backup was taken with. It pauses the Dagster daemons and refuses while a run is in flight.
    Each bucket is made equal to the backup's: objects written after it are deleted. The passphrase comes from a
    prompt or the environment variable `olf infra backup` documents. If a step fails, the workloads stay scaled to 0
    and the error says how to retry. The reconcile deploy builds the project from the current checkout, so run it
    from the checkout the backup was taken with.
    """
    import olf
    from olf.common import config
    from olf.common.deployment.errors import DeploymentError
    from olf.infra import k8s
    from olf.infra.backup import BackupError
    from olf.infra.backup._bundle import read_passphrase
    from olf.infra.backup._cluster import dagster_runs_in_flight
    from olf.infra.backup._restore import restore as restore_backup
    from olf.infra.deployment.engine import DeploymentPhase

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
            restore_backup(
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
        # The platform bootstrap keeps the restored principals. The artifacts phase builds a revision from the
        # checkout and activates it (`local/artifacts.py`), so when the checkout is newer than the backup its code,
        # not the restored pointer's revision, ends up running.
        _build_engine(context, var_file="").deploy(DeploymentPhase.ALL)
    except (BackupError, DeploymentError, k8s.KubectlError) as exc:
        raise typer.Exit(code=fail(str(exc))) from exc
