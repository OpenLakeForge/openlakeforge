"""CLI integration tests for `olf infra doctor --json`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from olf.cli import app

runner = CliRunner()


def test_doctor_json_output_has_valid_schema_on_error() -> None:
    """doctor --json emits valid JSON schema even when checks fail."""
    # Run doctor without valid project setup - will fail but should emit JSON
    result = runner.invoke(app, ["infra", "doctor", "--json"], catch_exceptions=False)

    # May fail (exit code 1), but must emit valid JSON
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Invalid JSON output: {exc}\nOutput: {result.stdout}")

    # Schema validation
    assert "command" in parsed
    assert "ok" in parsed
    assert "items" in parsed
    assert isinstance(parsed["items"], list)

    # If there are error items, they should have required fields
    for item in parsed["items"]:
        assert "id" in item
        assert "status" in item
        assert "message" in item
        assert "remedy" in item


def test_doctor_json_progress_not_in_stdout(tmp_path: Path) -> None:
    """doctor --json redirects progress to stderr, not stdout."""
    # Create minimal project structure
    (tmp_path / "openlakeforge.yaml").write_text("name: test\n")

    result = runner.invoke(app, ["infra", "doctor", "--json", "--project-root", str(tmp_path)])

    # stdout should only contain JSON, no progress messages
    lines = result.stdout.strip().split("\n")
    # First line should be valid JSON start
    assert lines[0].startswith("{") or lines[0].startswith(" ")

    # Parse entire output as JSON to verify no text leaked
    try:
        parsed = json.loads(result.stdout)
        assert "command" in parsed
    except json.JSONDecodeError:
        pytest.fail(f"Progress text leaked into JSON stdout: {result.stdout}")


def test_doctor_json_output_structure() -> None:
    """doctor --json output follows the documented schema structure."""
    result = runner.invoke(app, ["infra", "doctor", "--json"])

    parsed = json.loads(result.stdout)

    # Top-level fields
    assert parsed["command"] == "doctor"
    assert isinstance(parsed["ok"], bool)
    assert isinstance(parsed["items"], list)

    # Item structure (if any items exist)
    for item in parsed["items"]:
        assert isinstance(item["id"], str)
        assert item["status"] in ["pass", "fail"]
        assert isinstance(item["message"], str)
        # remedy can be None or str
        assert item["remedy"] is None or isinstance(item["remedy"], str)
