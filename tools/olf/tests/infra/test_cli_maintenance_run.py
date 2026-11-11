from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from olf.cli import app
from olf.common import log
from olf.infra import maintenance
from olf.infra.e2e._shell import E2EError

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[4]

_SILVER = {
    "dry_run": True,
    "tables": [
        {
            "entity": "orders",
            "table": "sales_silver.orders",
            "expired_snapshots": 3,
            "kept_snapshots": 10,
            "orphan_files": ["s3://a", "s3://b"],
            "orphan_bytes": 2048,
        }
    ],
}
_GOLD = {
    "dry_run": True,
    "results": [
        {"schema": "sales_gold", "table": "daily", "dry_run": True, "snapshots_past_window": 4},
        {"schema": "sales_gold", "table": "weekly", "dry_run": True, "error": "Access Denied"},
    ],
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(ROOT / "lakehouse_code", tmp_path / "lakehouse_code")
    profile = tmp_path / "openlakeforge.yaml"
    profile.write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: DeploymentProfile\nmetadata:\n  name: run-test\n"
        "spec:\n  provider:\n    type: local\n  preset: slim\n  stages:\n    dev:\n      enabled: true\n"
        "    prod:\n      enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("olf.common.contract_state.load_provider_contracts", lambda *_a, **_k: None)
    monkeypatch.setattr("olf.infra.contracts.build_contract_env", lambda *_a, **_k: ({}, []))
    monkeypatch.setattr("olf.infra.e2e._runner.prepare_kube_context", lambda _cfg, _kubeconfig: None)
    monkeypatch.setattr(maintenance, "dagster_client", lambda _cfg: contextlib.nullcontext(object()))
    return profile


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool]]:
    """Dagster: each launch is recorded as (job, op, dry_run); a job answers with its canned metadata."""
    seen: list[tuple[str, str, bool]] = []

    def run_job(_cfg: object, _client: object, job: str, op: str, dry_run: bool) -> tuple[str, dict[str, Any]]:
        log.info(f"{job}: launched (run-1)")
        seen.append((job, op, dry_run))
        metadata = _SILVER if job == "silver_maintenance" else _GOLD
        return "run-1", {**metadata, "dry_run": dry_run}

    monkeypatch.setattr(maintenance, "run_job", run_job)
    return seen


def test_a_silver_dry_run_reports_each_tables_snapshots_and_orphans(project: Path, launched: list) -> None:
    result = runner.invoke(
        app, ["infra", "maintenance", "run", "--layer", "silver", "--dry-run", "--stage", "prod", "-f", str(project)]
    )

    assert result.exit_code == 0, result.output
    assert launched == [("silver_maintenance", "maintain_silver", True)]
    assert "silver_maintenance on prod (dry run, nothing deleted)" in result.output
    row = next(line.split() for line in result.output.splitlines() if line.startswith("sales_silver.orders"))
    assert row == ["sales_silver.orders", "13", "13", "3", "2"]


def test_a_real_run_reports_the_snapshots_that_remain(project: Path, launched: list) -> None:
    result = runner.invoke(app, ["infra", "maintenance", "run", "--layer", "silver", "--json", "-f", str(project)])

    assert result.exit_code == 0, result.output
    assert launched == [("silver_maintenance", "maintain_silver", False)]
    (table,) = json.loads(result.stdout)["tables"]
    assert (table["snapshots_before"], table["snapshots_after"], table["snapshots_expired"]) == (13, 10, 3)


def test_json_stdout_holds_only_the_report_and_has_a_stable_shape(project: Path, launched: list) -> None:
    result = runner.invoke(
        app, ["infra", "maintenance", "run", "--layer", "gold", "--dry-run", "--json", "-f", str(project)]
    )

    assert result.exit_code == 0, result.output
    assert "launched" not in result.stdout
    report = json.loads(result.stdout)
    assert list(report) == ["layer", "stage", "job", "run_id", "dry_run", "tables"]
    assert (report["layer"], report["stage"], report["job"], report["dry_run"]) == (
        "gold",
        "dev",
        "gold_maintenance",
        True,
    )
    assert report["tables"][0] == {
        "table": "sales_gold.daily",
        "snapshots_before": None,
        "snapshots_after": None,
        "snapshots_expired": 4,
        "orphan_files": None,
        "orphan_bytes": None,
        "error": None,
    }
    assert report["tables"][1]["error"] == "Access Denied"


def test_a_failed_run_exits_non_zero_with_the_reason(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_args: object) -> tuple[str, dict[str, Any]]:
        raise E2EError("gold_maintenance run run-1 failed: Gold maintenance failed for 1 of 2 tables")

    monkeypatch.setattr(maintenance, "run_job", failing)

    result = runner.invoke(app, ["infra", "maintenance", "run", "--layer", "gold", "-f", str(project)])

    assert result.exit_code != 0
    assert "Gold maintenance failed for 1 of 2 tables" in result.output


def test_an_unknown_layer_launches_nothing(project: Path, launched: list) -> None:
    result = runner.invoke(app, ["infra", "maintenance", "run", "--layer", "bronze", "-f", str(project)])

    assert result.exit_code != 0
    assert "silver or gold" in result.output
    assert launched == []
