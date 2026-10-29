from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import E2E_REPO_ROOT
from typer.testing import CliRunner

from olf import bench
from olf.cli import app

runner = CliRunner()


def _invoke(tmp_path: Path, *args: str):  # noqa: ANN202
    return runner.invoke(app, ["bench", *args, "--project-root", str(E2E_REPO_ROOT), "--output", str(tmp_path / "out")])


def test_a_completed_run_writes_an_ok_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def etl(record, target, *, session, scale, delta, sample_interval):  # noqa: ANN001, ANN202
        seen.update(scale=scale, delta=delta, project_root=target.project_root)
        with record.timed("initial_order_revenue"):
            pass

    monkeypatch.setattr(bench, "etl", etl)

    result = _invoke(tmp_path, "etl", "--scale", "50", "--delta", "0")

    assert result.exit_code == 0, result.output
    document = json.loads((tmp_path / "out" / "result.json").read_text())
    assert document["status"] == "ok"
    assert document["scenario"] == "etl-x50"
    assert document["environment"]["parameters"]["scale"] == 50
    assert seen == {"scale": 50, "delta": 0, "project_root": E2E_REPO_ROOT}


def test_a_failed_run_exits_non_zero_and_still_writes_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def footprint(record, target, **_kwargs):  # noqa: ANN001, ANN202
        with record.timed("deploy_foundation"):
            raise bench.BenchError("kubelet did not start")

    monkeypatch.setattr(bench, "footprint", footprint)

    result = _invoke(tmp_path, "footprint", "--idle-seconds", "1")

    assert result.exit_code == 1
    document = json.loads((tmp_path / "out" / "result.json").read_text())
    assert document["status"] == "failed"
    assert document["measurements"][0]["error"] == "kubelet did not start"


def test_existing_measures_the_running_deployment_without_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def footprint(record, target, **kwargs):  # noqa: ANN001, ANN202
        seen.update(kwargs)

    monkeypatch.setattr(bench, "footprint", footprint)

    result = _invoke(tmp_path, "footprint", "--existing")

    assert result.exit_code == 0, result.output
    assert seen["install"] is False
    assert json.loads((tmp_path / "out" / "result.json").read_text())["environment"]["parameters"]["existing"] is True


def test_clean_and_existing_cannot_be_combined(tmp_path: Path) -> None:
    result = _invoke(tmp_path, "footprint", "--clean", "--existing")

    assert result.exit_code == 1
    assert not (tmp_path / "out" / "result.json").exists()
