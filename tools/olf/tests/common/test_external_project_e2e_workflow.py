"""The external project e2e workflow (#557) keeps its order and stays off GHCR and off secrets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO / ".github/workflows/external-project-e2e.yml"
_UPGRADE_DRILL = _REPO / ".github/workflows/upgrade-drill.yml"
_NIGHTLY = _REPO / ".github/workflows/local-full-e2e.yml"


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text())


def _steps() -> list[dict[str, Any]]:
    return _load(_WORKFLOW)["jobs"]["e2e"]["steps"]


def _index(steps: list[dict[str, Any]], command: str) -> int:
    (index,) = [i for i, step in enumerate(steps) if command in step.get("run", "")]
    return index


def test_the_workflow_is_dispatchable_with_an_acme_ref_and_runs_on_its_own_schedule() -> None:
    triggers = _load(_WORKFLOW)[True]  # PyYAML reads the key `on` as a boolean

    assert triggers["workflow_dispatch"]["inputs"]["acme_ref"]["default"] == "main"
    (schedule,) = triggers["schedule"]
    others = [_load(path)[True]["schedule"][0]["cron"] for path in (_UPGRADE_DRILL, _NIGHTLY)]
    assert schedule["cron"] not in others


def test_the_workflow_binds_sources_before_activating_and_runs_the_product_before_the_query() -> None:
    steps = _steps()
    platform = _index(steps, "for phase in foundation prefetch platform")
    bind = _index(steps, "olf infra source bind open_food_facts")
    artifacts = _index(steps, "--phase artifacts")
    product = _index(steps, "olf infra product run")
    query = _index(steps, "olf infra query")

    assert platform < bind < artifacts < product < query


def test_the_workflow_deploys_acme_from_this_checkout_without_pushing_or_secrets() -> None:
    text = _WORKFLOW.read_text()
    document = _load(_WORKFLOW)

    assert "repository: OpenLakeForge/acme-lakehouse" in text
    assert "source: ${{ github.workspace }}" in text
    assert document["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "ghcr.io" not in text
    assert "docker login" not in text
