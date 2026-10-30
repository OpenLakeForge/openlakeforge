"""A minimal Superset report bundle, shared by the validator and import/export tests."""

from __future__ import annotations

from pathlib import Path

REPORT_DIR = "lakehouse_code/dashboards/superset/demo"
DATABASE_UUID = "11111111-1111-5111-8111-111111111111"
DATASET_UUID = "22222222-2222-5222-8222-222222222222"
CHART_UUID = "33333333-3333-5333-8333-333333333333"
DASHBOARD_UUID = "44444444-4444-5444-8444-444444444444"


def write_report_bundle(
    root: Path,
    report_dir: str = REPORT_DIR,
    *,
    database_uuid: str = DATABASE_UUID,
    dataset_uuid: str = DATASET_UUID,
    chart_uuid: str = CHART_UUID,
    dashboard_uuid: str = DASHBOARD_UUID,
) -> Path:
    """A minimal promotable bundle shaped like a real Superset asset export."""
    bundle = root / report_dir
    for kind in ("databases", "datasets", "charts", "dashboards"):
        (bundle / kind).mkdir(parents=True)
    (bundle / "metadata.yaml").write_text("version: 1.0.0\ntype: assets\n")
    (bundle / "databases" / "trino.yaml").write_text(
        f"database_name: Trino\nsqlalchemy_uri: trino://superset@trino:8080/iceberg\nuuid: {database_uuid}\n"
    )
    (bundle / "datasets" / "mart.yaml").write_text(
        f"table_name: mart_orders\nschema: order_revenue_gold\nuuid: {dataset_uuid}\n"
        f"database_uuid: {database_uuid}\n"
    )
    (bundle / "charts" / "chart.yaml").write_text(
        f"slice_name: Orders\nuuid: {chart_uuid}\ndataset_uuid: {dataset_uuid}\n"
    )
    (bundle / "dashboards" / "dash.yaml").write_text(
        f"dashboard_title: Orders\nslug: orders\nuuid: {dashboard_uuid}\n"
        "position:\n"
        "  CHART-ORDERS:\n"
        "    id: CHART-ORDERS\n"
        "    type: CHART\n"
        "    meta:\n"
        f"      uuid: {chart_uuid}\n"
    )
    return bundle
