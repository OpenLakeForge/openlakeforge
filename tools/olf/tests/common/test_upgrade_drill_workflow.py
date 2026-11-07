"""The upgrade drill workflow (#912) keeps its order: capture before the upgrade, verify after it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO / ".github/workflows/upgrade-drill.yml"


def _document() -> dict[Any, Any]:
    return yaml.safe_load(_WORKFLOW.read_text())


def _steps() -> list[dict[str, str]]:
    return _document()["jobs"]["drill"]["steps"]


def _index(steps: list[dict[str, str]], command: str) -> int:
    (index,) = [i for i, step in enumerate(steps) if command in step.get("run", "")]
    return index


def test_the_drill_is_dispatchable_with_a_base_tag_and_runs_on_a_schedule() -> None:
    triggers = _document()[True]  # PyYAML reads the key `on` as a boolean

    assert triggers["workflow_dispatch"]["inputs"]["base_tag"]["default"] == ""
    assert triggers["schedule"]


def test_the_drill_captures_before_the_upgrade_and_verifies_after_it() -> None:
    steps = _steps()
    deploy_base = _index(steps, "olf init --preset full")
    capture = _index(steps, "durability capture")
    upgrade = _index(steps, "4-deploy-upgrade.log")
    verify = _index(steps, "durability verify")
    gold = _index(steps, "6-gold.log")

    assert deploy_base < capture < upgrade < verify < gold


def test_the_drill_runs_the_base_release_first_and_the_current_olf_for_the_upgrade() -> None:
    steps = {step["name"]: step["run"] for step in _steps() if "run" in step}

    assert "OLF_BASE_BIN" in steps["1. Deploy the base release with the project loaded"]
    assert "OLF_HEAD_BIN" in steps["3. Capture the durability baseline"]
    assert "OLF_HEAD_BIN" in steps["4. Upgrade in place with the current olf"]
