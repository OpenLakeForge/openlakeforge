from pathlib import Path

import pytest
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_services.governance import MetadataReconciler
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.infra.openmetadata._config import OpenMetadataConfig

# `build_contract_env` writes these only where a provider contract was
# actually applied for the stage being deployed; `OpenMetadataConfig` refuses
# an environment that carries neither.
APPLIED_DEV_CONTRACT = {
    "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
}



def _single_product_reconciler(tmp_path: Path) -> MetadataReconciler:
    (tmp_path / "bronze" / "crm").mkdir(parents=True)
    (tmp_path / "bronze" / "crm" / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1beta1
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
    (tmp_path / "governance.yml").write_text(
        """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    description: Sales domain
    products:
      - id: order_revenue
        displayName: Sales Order Revenue
        description: Revenue from orders.
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
    return MetadataReconciler(cfg, OpenMetadataClient(cfg.base_url))


def test_ensure_database_schema_creates_the_schema_with_its_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1 stopped pre-creating databaseSchemas when Polaris namespace
    lifecycle moved to Phase 2, so seeding has to create them itself."""
    reconciler = _single_product_reconciler(tmp_path)
    fake = FakeOpenMetadata()
    domain = fake.seed("domains", "sales", {})
    monkeypatch.setattr(reconciler.client, "request", fake.request)
    fqn = "polaris.lakehouse_dev.sales_order_revenue_silver"

    reconciler.ensure_database_schema(fqn, domain)

    schema = fake.entities["databaseSchemas"][fqn]
    assert [ref["id"] for ref in schema["domains"]] == [domain["id"]]
    assert schema["database"] == "polaris.lakehouse_dev"
    assert schema["extension"] == {"olfManaged": "git:unknown"}


def test_list_schema_tables_follows_paging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reconciler = _single_product_reconciler(tmp_path)
    pages = {
        "": {"data": [{"id": "a"}], "paging": {"after": "cursor"}},
        "cursor": {"data": [{"id": "b"}], "paging": {}},
    }

    def request(method: str, path: str, **_kwargs):
        return pages[path.partition("&after=")[2]]

    monkeypatch.setattr(reconciler.client, "request", request)

    assert reconciler.list_schema_tables("polaris.lakehouse_dev.order_revenue_gold") == [
        {"id": "a", "type": "table"},
        {"id": "b", "type": "table"},
    ]


def test_ensure_database_schema_is_reconciled_once_per_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciler = _single_product_reconciler(tmp_path)
    fake = FakeOpenMetadata()
    monkeypatch.setattr(reconciler.client, "request", fake.request)

    reconciler.ensure_database_schema("polaris.lakehouse_dev.sales_order_revenue_silver")
    fake.calls.clear()
    reconciler.ensure_database_schema("polaris.lakehouse_dev.sales_order_revenue_silver")

    assert fake.calls == []


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
        lambda: reconciler.list_schema_tables("polaris.lakehouse_prod.sales_silver"),
    ):
        with pytest.raises(OpenMetadataError, match="belongs to another stage"):
            call()

    assert requests == []
