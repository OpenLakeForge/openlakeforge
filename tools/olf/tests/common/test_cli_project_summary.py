"""`olf project summary` against descriptors on disk."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_floe_entity
from typer.testing import CliRunner

from olf.cli import app
from olf.common.project_summary import render_project_summary_text

ROOT = Path(__file__).parents[4]

runner = CliRunner()

_GOVERNANCE = """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: planned
    products:
      - id: revenue
        displayName: Revenue
        description: Revenue product.
        status: planned
"""

_SOURCE = """\
apiVersion: openlakeforge.io/v1beta1
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: orders
"""


@pytest.fixture
def tiny_project(tmp_path: Path) -> Path:
    code = tmp_path / "lakehouse_code"
    (code / "bronze/crm").mkdir(parents=True)
    (code / "governance.yml").write_text(_GOVERNANCE)
    board = code / "dashboards/superset/revenue_board"
    (board / "datasets/Trino").mkdir(parents=True)
    (board / "metadata.yaml").write_text("type: assets\n")
    (board / "datasets/Trino/mart.yaml").write_text("schema: revenue_gold\n")
    (code / "bronze/crm/source.yaml").write_text(_SOURCE)
    write_floe_entity(code, "sales", "orders", "crm")
    models = code / "gold/models/revenue"
    models.mkdir(parents=True)
    (models / "mart_revenue.sql").write_text("SELECT 1;\n")
    return tmp_path


def test_summary_prints_the_project_inventory(tiny_project: Path) -> None:
    result = runner.invoke(app, ["project", "summary", "--project", str(tiny_project)])

    assert result.exit_code == 0, result.output
    assert result.output == (
        "Sources:\n"
        "  crm: orders\n"
        "Domains:\n"
        "  sales: 1 Silver table(s)\n"
        "Products:\n"
        "  revenue: 1 Gold mart(s)\n"
        "Dashboards:\n"
        "  revenue_board\n"
    )


def test_summary_emits_deterministic_json(tiny_project: Path) -> None:
    first = runner.invoke(app, ["project", "summary", "--project", str(tiny_project), "--json"])
    second = runner.invoke(app, ["project", "summary", "--project", str(tiny_project), "--json"])

    assert first.exit_code == 0, first.output
    assert first.output == second.output

    data = json.loads(first.output)
    assert data == {
        "dashboards": ["revenue_board"],
        "domains": [{"name": "sales", "silver_tables": 1}],
        "products": [{"gold_marts": 1, "name": "revenue"}],
        "sources": [{"name": "crm", "resources": ["orders"]}],
    }


def test_summary_fails_without_a_lakehouse_descriptor(tmp_path: Path) -> None:
    result = runner.invoke(app, ["project", "summary", "--project", str(tmp_path)])

    assert result.exit_code != 0
    assert "governance.yml" in result.output


def test_summary_of_the_checked_in_project_reflects_all_layers() -> None:
    text_result = runner.invoke(app, ["project", "summary", "--project", str(ROOT)])
    assert text_result.exit_code == 0, text_result.output

    assert "Sources:" in text_result.output
    assert "crm: accounts," in text_result.output
    assert "erp: inventory_snapshots," in text_result.output
    assert "sales: 9 Silver table(s)" in text_result.output
    assert "supply_chain: 6 Silver table(s)" in text_result.output
    assert "customer_health: 3 Gold mart(s)" in text_result.output
    assert "inventory_reliability: 3 Gold mart(s)" in text_result.output
    assert "order_revenue: 4 Gold mart(s)" in text_result.output
    assert "sales_customer_health" in text_result.output
    assert "sales_order_revenue" in text_result.output
    assert "supply_chain_inventory_reliability" in text_result.output

    json_result = runner.invoke(app, ["project", "summary", "--project", str(ROOT), "--json"])
    assert json_result.exit_code == 0, json_result.output
    json_data = json.loads(json_result.output)

    assert [s["name"] for s in json_data["sources"]] == ["crm", "erp"]
    assert [d["name"] for d in json_data["domains"]] == ["sales", "supply_chain"]
    assert json_data["domains"][0]["silver_tables"] == 9
    assert json_data["domains"][1]["silver_tables"] == 6
    assert [p["name"] for p in json_data["products"]] == [
        "customer_health",
        "inventory_reliability",
        "order_revenue",
    ]
    assert [p["gold_marts"] for p in json_data["products"]] == [3, 3, 4]
    assert json_data["dashboards"] == [
        "sales_customer_health",
        "sales_order_revenue",
        "supply_chain_inventory_reliability",
    ]


def test_render_project_summary_text_handles_empty_sections() -> None:
    empty_inventory = SimpleNamespace(
        sources=(),
        domains=(),
        products=(),
        dashboards=(),
    )
    rendered = render_project_summary_text(empty_inventory)  # type: ignore[arg-type]
    assert rendered == (
        "Sources:\n"
        "  (none)\n"
        "Domains:\n"
        "  (none)\n"
        "Products:\n"
        "  (none)\n"
        "Dashboards:\n"
        "  (none)\n"
    )

