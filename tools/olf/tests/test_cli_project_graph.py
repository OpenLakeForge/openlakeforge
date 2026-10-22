"""`olf project graph` against descriptors on disk."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from olf.cli import app

ROOT = Path(__file__).parents[3]

runner = CliRunner()

_LAKEHOUSE = """\
apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: tiny
displayName: Tiny
description: Tiny lakehouse.
status: planned
sources:
  - crm
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: planned
    silver_tables:
      tables:
        - name: orders
          source: crm
          resource: orders
    products:
      - id: revenue
        displayName: Revenue
        description: Revenue product.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_revenue
dashboards:
  - name: revenue_board
    products: [revenue]
"""

_SOURCE = """\
apiVersion: openlakeforge.io/v1alpha3
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
    (code / "lakehouse.yaml").write_text(_LAKEHOUSE)
    (code / "bronze/crm/source.yaml").write_text(_SOURCE)
    return tmp_path


def test_graph_prints_the_project_lineage(tiny_project: Path) -> None:
    result = runner.invoke(app, ["project", "graph", "--project", str(tiny_project)])

    assert result.exit_code == 0, result.output
    assert result.output == (
        "flowchart LR\n"
        '    dashboard_revenue_board["revenue_board"]\n'
        '    domain_sales["Sales"]\n'
        '    product_revenue["Revenue"]\n'
        '    source_crm["CRM"]\n'
        "    domain_sales --> product_revenue\n"
        "    product_revenue --> dashboard_revenue_board\n"
        "    source_crm --> domain_sales\n"
    )


def test_graph_fails_without_a_lakehouse_descriptor(tmp_path: Path) -> None:
    result = runner.invoke(app, ["project", "graph", "--project", str(tmp_path)])

    assert result.exit_code != 0
    assert "lakehouse.yaml" in result.output


def test_graph_of_the_demo_project_names_every_owner_and_is_repeatable() -> None:
    first = runner.invoke(app, ["project", "graph", "--project", str(ROOT)])
    second = runner.invoke(app, ["project", "graph", "--project", str(ROOT)])

    assert first.exit_code == 0, first.output
    assert first.output == second.output
    for node in (
        "source_crm",
        "source_erp",
        "domain_sales",
        "domain_supply_chain",
        "product_order_revenue",
        "product_customer_health",
        "product_inventory_reliability",
        "dashboard_sales_order_revenue",
        "dashboard_sales_customer_health",
        "dashboard_supply_chain_inventory_reliability",
    ):
        assert f"    {node}[" in first.output
