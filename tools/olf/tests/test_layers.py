from __future__ import annotations

import pytest

from olf.layers import deploy_enabled_artifacts, enabled


def test_deploy_enabled_artifacts_only_runs_enabled_layers() -> None:
    deployed: list[str] = []
    messages: list[str] = []

    deploy_enabled_artifacts(
        {"OPENLAKEFORGE_ANALYTICS_ENABLED": "false", "OPENLAKEFORGE_GOVERNANCE_ENABLED": "false"},
        deploy_reports=lambda: deployed.append("reports"),
        deploy_metadata=lambda: deployed.append("metadata"),
        report=messages.append,
    )

    assert deployed == []
    assert messages == [
        "Skipping Superset report assets: analytics layer is disabled.",
        "Skipping OpenMetadata governance metadata: governance layer is disabled.",
    ]


def test_deploy_enabled_artifacts_runs_both_layers_by_default() -> None:
    deployed: list[str] = []

    deploy_enabled_artifacts(
        {},
        deploy_reports=lambda: deployed.append("reports"),
        deploy_metadata=lambda: deployed.append("metadata"),
        report=lambda _message: None,
    )

    assert deployed == ["reports", "metadata"]


def test_enabled_rejects_unknown_layer() -> None:
    with pytest.raises(ValueError, match="unknown optional layer"):
        enabled({}, "catalog")
