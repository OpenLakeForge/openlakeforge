from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from olf.cli import app

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(ROOT / "lakehouse_code", tmp_path / "lakehouse_code")
    profile = tmp_path / "openlakeforge.yaml"
    profile.write_text(
        "apiVersion: openlakeforge.io/v1alpha1\nkind: DeploymentProfile\nmetadata:\n  name: run-test\n"
        "spec:\n  provider:\n    type: local\n  preset: slim\n  stages:\n    dev:\n      enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("olf.contract_state.load_provider_contracts", lambda *_a, **_k: None)
    monkeypatch.setattr("olf.contracts.build_contract_env", lambda *_a, **_k: ({}, []))
    monkeypatch.setattr("olf.e2e._runner.prepare_kube_context", lambda _cfg: None)
    return profile


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    def launch_and_poll(cfg, *, products=None, tags=None):  # noqa: ANN001, ANN202
        seen.append({"products": [product.id for product in products], "tags": tags})

    monkeypatch.setattr("olf.e2e._dagster.launch_and_poll_dagster_jobs", launch_and_poll)
    return seen


def test_a_product_runs_incrementally_by_default(project: Path, launches: list[dict]) -> None:
    result = runner.invoke(app, ["product", "run", "order_revenue", "-f", str(project)])

    assert result.exit_code == 0, result.output
    assert launches == [{"products": ["order_revenue"], "tags": None}]


def test_a_full_refresh_is_tagged_on_that_run_only(project: Path, launches: list[dict]) -> None:
    result = runner.invoke(app, ["product", "run", "order_revenue", "-f", str(project), "--full-refresh", "bronze"])

    assert result.exit_code == 0, result.output
    assert launches == [{"products": ["order_revenue"], "tags": {"openlakeforge/full_refresh": "bronze"}}]


def test_an_unknown_full_refresh_scope_launches_nothing(project: Path, launches: list[dict]) -> None:
    result = runner.invoke(app, ["product", "run", "order_revenue", "-f", str(project), "--full-refresh", "silver"])

    assert result.exit_code != 0
    assert "bronze or gold" in result.output
    assert launches == []


def test_an_unknown_product_names_the_products_there_are(project: Path, launches: list[dict]) -> None:
    result = runner.invoke(app, ["product", "run", "revenue", "-f", str(project)])

    assert result.exit_code != 0
    assert "order_revenue" in result.output
    assert launches == []
