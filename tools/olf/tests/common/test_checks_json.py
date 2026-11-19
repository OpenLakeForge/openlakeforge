from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from olf.cli import app
from olf.common.log import fail

runner = CliRunner()

_CHECK_KEYS = {"id", "status", "message", "remedy"}


def _document(stdout: str) -> dict:
    document = json.loads(stdout)
    assert set(document) == {"command", "ok", "checks"}
    assert all(set(check) == _CHECK_KEYS for check in document["checks"])
    return document


def test_a_passing_target_prints_only_the_document_on_stdout(tmp_path: Path) -> None:
    (tmp_path / ".trivyignore.yaml").write_text("# nothing suppressed\n")

    result = runner.invoke(app, ["check", "security", "--repo-root", str(tmp_path), "--json"])

    assert result.exit_code == 0
    assert _document(result.stdout) == {
        "command": "check security",
        "ok": True,
        "checks": [
            {
                "id": "security",
                "status": "pass",
                "message": "Security suppressions are owned and unexpired.",
                "remedy": None,
            }
        ],
    }
    assert "Security suppressions are owned and unexpired." in result.stderr


def test_a_failing_target_keeps_its_exit_code_and_reports_its_error(tmp_path: Path) -> None:
    (tmp_path / ".trivyignore.yaml").write_text("misconfigurations:\n  - {id: D, owner: x}\n")

    result = runner.invoke(app, ["check", "security", "--repo-root", str(tmp_path), "--json"])

    assert result.exit_code == 1
    document = _document(result.stdout)
    assert document["ok"] is False
    (check,) = document["checks"]
    assert check["status"] == "fail"
    assert "D: expired_at must be a YYYY-MM-DD date" in check["message"]
    assert not check["message"].startswith("ERROR")
    assert "ERROR:" in result.stderr


def test_a_failing_report_without_an_error_line_carries_the_report(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", "contracts", "--repo-root", str(tmp_path), "--json"])

    assert result.exit_code == 1
    (check,) = _document(result.stdout)["checks"]
    assert check["status"] == "fail"
    assert "[FAIL] descriptor_schema_conformance" in check["message"]


def test_without_json_the_text_output_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".trivyignore.yaml").write_text("# nothing suppressed\n")

    result = runner.invoke(app, ["check", "security", "--repo-root", str(tmp_path)])

    assert result.exit_code == 0
    assert result.stdout == "Security suppressions are owned and unexpired.\n"


def test_check_all_stops_at_the_first_failure_and_lists_the_rest_as_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from olf.common.commands import checks

    def passing(*_args: object) -> None:
        typer.echo("fine")

    def failing(*_args: object) -> None:
        raise typer.Exit(code=fail("components drifted"))

    for name in (
        "structure",
        "docs",
        "skills",
        "contracts",
        "flows",
        "infra",
        "resources",
        "disruption",
        "project_code",
        "dbt",
        "lockfiles",
    ):
        monkeypatch.setattr(checks, name, passing)
    monkeypatch.setattr(checks, "components", failing)

    result = runner.invoke(app, ["check", "all", "--json"])

    assert result.exit_code == 1
    document = _document(result.stdout)
    assert document["command"] == "check all"
    assert document["ok"] is False
    statuses = {check["id"]: check["status"] for check in document["checks"]}
    assert statuses == {
        "structure": "pass",
        "docs": "pass",
        "skills": "pass",
        "components": "fail",
        "contracts": "skipped",
        "flows": "skipped",
        "infra": "skipped",
        "resources": "skipped",
        "disruption": "skipped",
        "project-code": "skipped",
        "dbt": "skipped",
        "lockfiles": "skipped",
    }
    assert document["checks"][3]["message"] == "components drifted"
