"""Provider-neutral dynamic artifact deployment steps.

Port of the shared portion of `scripts/{local,aws,azure}/stack/
deploy-artifacts.sh`: catalog namespace reconciliation and optional
Superset/OpenMetadata layer deployment, kept out of `olf.common.deployment.local`
so the AWS/Azure providers (#125) reuse it unmodified.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from olf.common import layers, log
from olf.common.deployment.errors import DeploymentPreconditionError


class ArtifactOperationError(DeploymentPreconditionError):
    """Raised when a called `olf` artifact operation fails."""


def _run_cli(description: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except typer.Exit as exc:
        raise ArtifactOperationError(f"{description} failed (see the ERROR above).") from exc


def sync_catalog_namespaces() -> None:
    # sync_namespaces, not the catalog_sync_namespaces typer command: this
    # already runs inside the deploy flow's own hydrated contract
    # environment (see local/artifacts.py, cloud/artifacts.py) - resolving
    # a second one here would be redundant and could pick a different
    # provider/project selection than the one already active.
    from olf.infra.commands.catalog import sync_namespaces

    _run_cli("Catalog namespace reconciliation", sync_namespaces, dry_run=False, prune=None)


def deploy_optional_layer_artifacts(environ: Mapping[str, str]) -> None:
    from olf.infra.commands.openmetadata import deploy_openmetadata_metadata
    from olf.infra.commands.superset import deploy_superset_reports

    layers.deploy_enabled_artifacts(
        environ,
        deploy_reports=deploy_superset_reports,
        deploy_metadata=deploy_openmetadata_metadata,
        report=log.step,
    )
