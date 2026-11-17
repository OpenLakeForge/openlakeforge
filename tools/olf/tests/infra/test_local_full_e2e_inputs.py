"""The manual runs of the local full e2e are opt-in; only the nightly runs everything (`local-full-e2e.yml`).

These tests read the checked-in YAML for the dispatch contract agents rely on: which inputs exist, that the heavy ones
default off, and that a custom profile reaches the job through the environment and not through the shell script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/local-full-e2e.yml").read_text())
# PyYAML reads the bare key `on` as True.
INPUTS: dict[str, Any] = WORKFLOW[True]["workflow_dispatch"]["inputs"]
STEPS: list[dict[str, Any]] = WORKFLOW["jobs"]["e2e"]["steps"]


def _step(name_prefix: str) -> dict[str, Any]:
    return next(step for step in STEPS if step["name"].startswith(name_prefix))


def test_a_manual_run_defaults_to_dev_only_core_checks_without_the_optional_drills() -> None:
    assert INPUTS["stage_scope"]["default"] == "dev-only"
    assert INPUTS["checks"]["default"] == "core"
    assert INPUTS["restore_drill"]["default"] is False
    assert INPUTS["stage_drill"]["default"] is False
    assert INPUTS["registry_pull_check"]["default"] is False


def test_checks_can_be_skipped_entirely() -> None:
    assert "none" in INPUTS["checks"]["options"]


def test_the_registry_pull_proof_runs_on_the_nightly_or_when_asked() -> None:
    condition = _step("Prove the stages pull the project image")["if"]

    assert "schedule" in condition
    assert "inputs.registry_pull_check" in condition


def test_a_custom_profile_arrives_as_text_or_as_a_path_and_is_kept_out_of_the_script() -> None:
    assert INPUTS["profile"]["type"] == "string"
    assert INPUTS["profile_path"]["type"] == "string"
    init = _step("Initialise an external project")

    assert init["env"]["PROFILE_YAML"] == "${{ inputs.profile }}"
    assert init["env"]["PROFILE_PATH"] == "${{ inputs.profile_path }}"
    assert "inputs.profile" not in init["run"]


def test_every_prod_step_keys_off_the_profile_not_the_stage_scope_input() -> None:
    for step in STEPS:
        if "PROD" in step["name"] and "if" in step:
            assert "inputs.stage_scope" not in step["if"], step["name"]
