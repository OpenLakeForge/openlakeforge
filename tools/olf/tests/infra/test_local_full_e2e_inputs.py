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


def test_the_nightly_gets_the_same_time_budget_as_a_manual_run() -> None:
    """A schedule has no inputs, so it takes the expression's fallback and not the input's default."""
    fallback = f"inputs.timeout_minutes || {INPUTS['timeout_minutes']['default']}"

    assert fallback in WORKFLOW["jobs"]["e2e"]["timeout-minutes"]


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


def _evaluate(expression: str, checks: str | None) -> str:
    """Evaluate the `${{ a == 'x' && 'y' || 'z' }}` forms this workflow uses, with GitHub's semantics.

    `&&` and `||` return an operand rather than a boolean, and an empty string or null is falsy, so
    `cond && '' || 'z'` is always `z`.
    """
    body = expression.removeprefix("${{").removesuffix("}}").strip()

    def operand(text: str) -> str | None:
        text = text.strip()
        if text.startswith("'"):
            return text.strip("'")
        for operator in ("==", "!="):
            if operator in text:
                left, right = (side.strip() for side in text.split(operator))
                assert left == "inputs.checks", left
                return str((checks == right.strip("'")) == (operator == "==")).lower()
        assert text == "inputs.checks", text
        return checks

    def truthy(value: str | None) -> bool:
        return value not in (None, "", "false")

    def conjunction(text: str) -> str | None:
        value: str | None = None
        for part in text.split("&&"):
            value = operand(part)
            if not truthy(value):
                return value
        return value

    value: str | None = None
    for part in body.split("||"):
        value = conjunction(part)
        if truthy(value):
            return value or ""
    return value or ""


def test_checks_none_deploys_without_the_e2e_suite_and_the_other_choices_run_it() -> None:
    with_ = _step("Activate the revision in DEV")["with"]

    # `None` is the scheduled run, where every input is null.
    environments = {checks: _evaluate(with_["e2e-env"], checks) for checks in ("none", "core", "all", None)}
    levels = {checks: _evaluate(with_["e2e-checks"], checks) for checks in ("core", "all", None)}

    assert environments == {"none": "", "core": "local", "all": "local", None: "local"}
    assert levels == {"core": "core", "all": "all", None: "all"}
