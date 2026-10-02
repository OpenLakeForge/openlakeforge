from pathlib import Path

import pytest

from olf.clients.openmetadata import OpenMetadataClient, OpenMetadataError
from olf.openmetadata._config import OpenMetadataConfig
from olf.openmetadata._reconciliation import OpenMetadataReconciler

# `build_contract_env` writes these only where a provider contract was
# actually applied for the stage being deployed; `OpenMetadataConfig` refuses
# an environment that carries neither.
APPLIED_DEV_CONTRACT = {
    "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
}



def _single_product_reconciler(tmp_path: Path) -> OpenMetadataReconciler:
    (tmp_path / "bronze" / "crm").mkdir(parents=True)
    (tmp_path / "bronze" / "crm" / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: crm source.
status: active
resources:
  - name: orders
    description: Raw sales orders.
"""
    )
    (tmp_path / "lakehouse.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: active
sources:
  - crm
domains:
  - name: sales
    displayName: Sales
    description: Sales domain
    status: active
    silver_tables:
      tables:
        - {name: raw_orders, source: crm, resource: orders}
        - {name: raw_order_lines, source: crm, resource: orders}
    products:
      - id: order_revenue
        displayName: Sales Order Revenue
        description: Revenue from orders.
        status: active
        silver_inputs: [raw_orders, raw_order_lines]
        gold_tables:
          tables:
            - name: mart_order_revenue
dashboards: []
"""
    )
    cfg = OpenMetadataConfig.from_environment(
        dict(APPLIED_DEV_CONTRACT),
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root=str(tmp_path),
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )
    return OpenMetadataReconciler(cfg, OpenMetadataClient(cfg.base_url))


def test_ensure_database_schema_creates_the_schema_a_table_will_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1 stopped pre-creating databaseSchemas when Polaris namespace
    lifecycle moved to Phase 2, so seeding has to create them itself."""
    reconciler = _single_product_reconciler(tmp_path)
    calls: list[tuple[str, dict]] = []

    def request(method: str, path: str, *, json_body=None, **_kwargs):
        calls.append((path, json_body or {}))
        return {}

    monkeypatch.setattr(reconciler.client, "request", request)

    reconciler.ensure_database_schema("polaris.lakehouse_dev.sales_order_revenue_silver")

    assert calls == [
        (
            "/api/v1/databaseSchemas",
            {"name": "sales_order_revenue_silver", "database": "polaris.lakehouse_dev"},
        )
    ]


def test_ensure_database_schema_is_created_once_per_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _single_product_reconciler(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        reconciler.client, "request", lambda method, path, **_kwargs: calls.append(path) or {}
    )

    reconciler.ensure_database_schema("polaris.lakehouse_dev.sales_order_revenue_silver")
    reconciler.ensure_database_schema("polaris.lakehouse_dev.sales_order_revenue_silver")

    assert calls == ["/api/v1/databaseSchemas"]


def test_ensure_database_schema_rejects_a_malformed_fqn(tmp_path: Path) -> None:
    reconciler = _single_product_reconciler(tmp_path)

    with pytest.raises(OpenMetadataError, match="Malformed schema FQN"):
        reconciler.ensure_database_schema("sales_order_revenue_silver")


def test_write_primitives_refuse_another_stages_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every governed stage shares one OpenMetadata, separated only by the
    `<service>.lakehouse_<stage>` database root, so a reconcile scoped to one
    stage must not create or claim entities under another's."""
    reconciler = _single_product_reconciler(tmp_path)
    requests: list[str] = []

    monkeypatch.setattr(
        reconciler.client, "request", lambda method, path, **_kwargs: requests.append(path) or {}
    )

    for call in (
        lambda: reconciler.ensure_database_schema("polaris.lakehouse_prod.sales_silver"),
        lambda: reconciler.ensure_table_stub("polaris.lakehouse_prod.sales_silver", "raw_orders", ""),
        lambda: reconciler.resolve_table_asset("polaris.lakehouse_prod.sales_silver.raw_orders"),
    ):
        with pytest.raises(OpenMetadataError, match="belongs to another stage"):
            call()

    assert requests == []
