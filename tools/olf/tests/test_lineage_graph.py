from __future__ import annotations

from pathlib import Path

from conftest import write_floe_entity
from openlakeforge_domain import Dashboard, Domain, LakehouseInventory, Product, Source, Table

from olf.lineage_graph import render_lineage_graph


def _inventory(
    root: Path,
    *,
    source_display_name: str = "CRM",
    domain_name: str = "sales",
    product_id: str = "revenue",
    dashboards: tuple[Dashboard, ...] = (),
) -> LakehouseInventory:
    product = Product(domain_name=domain_name, id=product_id, display_name="Revenue")
    for name in ("orders", "order_lines"):
        write_floe_entity(root, domain_name, name, "crm")
    domain = Domain(
        descriptor_path=Path("lakehouse.yaml"),
        name=domain_name,
        display_name="Sales",
        products=(product,),
    )
    source = Source(
        descriptor_path=Path("bronze/crm/source.yaml"),
        name="crm",
        display_name=source_display_name,
        resources=(Table("orders"), Table("order_lines")),
    )
    return LakehouseInventory(
        lakehouse_root=root, name="test", sources=(source,), domains=(domain,), dashboards=dashboards
    )


def test_graph_links_source_to_domain_to_product_to_dashboard(tmp_path: Path) -> None:
    boards = (Dashboard(name="revenue_board", products=("revenue",)),)
    graph = render_lineage_graph(_inventory(tmp_path, dashboards=boards))

    assert graph == (
        "flowchart LR\n"
        '    dashboard_revenue_board["revenue_board"]\n'
        '    domain_sales["Sales"]\n'
        '    product_revenue["Revenue"]\n'
        '    source_crm["CRM"]\n'
        "    domain_sales --> product_revenue\n"
        "    product_revenue --> dashboard_revenue_board\n"
        "    source_crm --> domain_sales\n"
    )


def test_two_tables_from_one_source_draw_one_edge(tmp_path: Path) -> None:
    graph = render_lineage_graph(_inventory(tmp_path))

    assert graph.count("source_crm --> domain_sales") == 1


def test_product_without_a_dashboard_has_no_dashboard_edge(tmp_path: Path) -> None:
    graph = render_lineage_graph(_inventory(tmp_path))

    assert "dashboard_" not in graph
    assert "domain_sales --> product_revenue" in graph


def test_domain_and_product_sharing_a_name_stay_separate_nodes(tmp_path: Path) -> None:
    graph = render_lineage_graph(_inventory(tmp_path, domain_name="sales", product_id="sales"))

    assert "domain_sales --> product_sales" in graph


def test_labels_escape_mermaid_syntax(tmp_path: Path) -> None:
    graph = render_lineage_graph(_inventory(tmp_path, source_display_name='CRM "#1" <eu>\nexport'))

    assert 'source_crm["CRM #quot;#35;1#quot; #lt;eu#gt; export"]' in graph


def test_output_does_not_depend_on_declaration_order(tmp_path: Path) -> None:
    boards = (
        Dashboard(name="b_board", products=("revenue",)),
        Dashboard(name="a_board", products=("revenue",)),
    )

    assert render_lineage_graph(_inventory(tmp_path, dashboards=boards)) == render_lineage_graph(
        _inventory(tmp_path, dashboards=tuple(reversed(boards)))
    )
