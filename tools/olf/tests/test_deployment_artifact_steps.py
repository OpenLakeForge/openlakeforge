from __future__ import annotations

from typing import Any

import pytest
import typer

from olf.deployment import artifact_steps
from olf.deployment.errors import DeploymentPreconditionError


def test_artifact_operation_error_is_a_deployment_precondition_error() -> None:
    assert issubclass(artifact_steps.ArtifactOperationError, DeploymentPreconditionError)


def test_sync_catalog_namespaces_wraps_typer_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raises_exit(*, dry_run: bool, prune: Any) -> None:  # noqa: ARG001
        raise typer.Exit(code=1)

    monkeypatch.setattr("olf.commands.catalog.sync_namespaces", _raises_exit)

    with pytest.raises(artifact_steps.ArtifactOperationError):
        artifact_steps.sync_catalog_namespaces()


def test_deploy_optional_layer_artifacts_delegates_to_layers_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        artifact_steps.layers,
        "deploy_enabled_artifacts",
        lambda environ, *, deploy_reports, deploy_metadata, report: calls.append("deploy_enabled_artifacts"),
    )
    monkeypatch.setattr("olf.commands.superset.deploy_superset_reports", lambda *a, **k: None)
    monkeypatch.setattr("olf.commands.openmetadata.deploy_openmetadata_metadata", lambda *a, **k: None)

    artifact_steps.deploy_optional_layer_artifacts({})

    assert calls == ["deploy_enabled_artifacts"]
