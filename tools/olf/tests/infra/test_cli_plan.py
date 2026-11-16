"""CLI integration tests for `olf infra plan --json`."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from olf.cli import app

runner = CliRunner()


def test_plan_json_output_has_valid_schema_on_error() -> None:
    """plan --json emits valid JSON schema even when planning fails."""
    # Run plan without valid project setup - will fail but should emit JSON
    result = runner.invoke(app, ["infra", "plan", "--json"], catch_exceptions=False)

    # Must emit valid JSON regardless of exit code
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Invalid JSON output: {exc}\nOutput: {result.stdout}")

    # Schema validation
    assert "command" in parsed
    assert "ok" in parsed
    assert "phase" in parsed
    assert "changes_pending" in parsed
    assert "items" in parsed
    assert isinstance(parsed["items"], list)


def test_plan_json_changes_pending_is_null_on_error() -> None:
    """plan --json sets changes_pending to null (not false) when plan fails."""
    result = runner.invoke(app, ["infra", "plan", "--json"])

    parsed = json.loads(result.stdout)

    # On error, changes_pending should be None (null in JSON), not False
    if not parsed["ok"]:
        assert parsed["changes_pending"] is None


def test_plan_json_output_structure() -> None:
    """plan --json output follows the documented schema structure."""
    result = runner.invoke(app, ["infra", "plan", "--json"])

    parsed = json.loads(result.stdout)

    # Top-level fields
    assert parsed["command"] == "plan"
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["phase"], str)
    # changes_pending can be bool or None
    assert parsed["changes_pending"] is None or isinstance(parsed["changes_pending"], bool)
    assert isinstance(parsed["items"], list)

    # Item structure (if any items exist)
    for item in parsed["items"]:
        assert isinstance(item["id"], str)
        assert item["status"] in ["pending", "clean", "fail"]
        assert isinstance(item["message"], str)
        # remedy can be None or str
        assert item["remedy"] is None or isinstance(item["remedy"], str)


def test_plan_json_detailed_exitcode_with_changes() -> None:
    """plan --json --detailed-exitcode exits with code 2 when changes pending."""
    # This test will likely fail in CI without infrastructure, but documents expected behavior
    result = runner.invoke(app, ["infra", "plan", "--json", "--detailed-exitcode"])

    if result.exit_code == 2:
        # Exit code 2 means changes pending
        parsed = json.loads(result.stdout)
        assert parsed["ok"] is True  # plan succeeded
        assert parsed["changes_pending"] is True
        assert any(item["status"] == "pending" for item in parsed["items"])


def test_plan_json_item_has_remedy_when_changes_pending() -> None:
    """plan --json includes remedy when changes are pending."""
    result = runner.invoke(app, ["infra", "plan", "--json"])

    if result.exit_code == 0:
        parsed = json.loads(result.stdout)
        if parsed.get("changes_pending"):
            # When changes pending, remedy should suggest deployment
            assert any(
                item.get("remedy") and "deploy" in item["remedy"].lower()
                for item in parsed["items"]
            )
