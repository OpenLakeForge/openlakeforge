from __future__ import annotations

import json
from pathlib import Path

import pytest

from olf.common.deployment.context import DeploymentContext, observability_variables
from olf.common.profile import DeploymentProfile, Preset, Provider, ProviderSpec, StageName, StageSpec, resolve_topology


def _variables(tmp_path: Path, preset: Preset) -> dict[str, object]:
    profile = DeploymentProfile(
        name="obs",
        provider=ProviderSpec(type=Provider.LOCAL),
        preset=preset,
        stages=(StageSpec(name=StageName.DEV, enabled=True),),
    )
    context = DeploymentContext.local(repo_root=tmp_path, topology=resolve_topology(profile))
    return json.loads(observability_variables(context)["observability"])


def test_slim_reaches_the_roots_as_an_archive_only_input(tmp_path: Path) -> None:
    assert _variables(tmp_path, Preset.SLIM) == {
        "archive_retention_days": 30,
        "search_enabled": False,
        "metrics_enabled": False,
        "metrics_retention_days": 7,
        "grafana_enabled": False,
        "alert_receivers": [],
    }


def test_full_differs_from_slim_only_by_metrics(tmp_path: Path) -> None:
    slim = _variables(tmp_path, Preset.SLIM)
    full = _variables(tmp_path, Preset.FULL)

    assert {key for key in slim if slim[key] != full[key]} == {"metrics_enabled"}
    assert full["metrics_enabled"] is True


@pytest.mark.parametrize("preset", [Preset.SLIM, Preset.FULL])
def test_no_secret_reference_reaches_the_input(tmp_path: Path, preset: Preset) -> None:
    assert "secret" not in json.dumps(_variables(tmp_path, preset))
