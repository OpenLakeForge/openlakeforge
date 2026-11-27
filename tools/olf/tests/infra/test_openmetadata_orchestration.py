import dataclasses
from pathlib import Path

import pytest
from conftest import write_floe_entity
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_services import governance

from olf.infra import openmetadata as om

# `build_contract_env` writes these only where a provider contract was
# actually applied for the stage being deployed; `OpenMetadataConfig` refuses
# an environment that carries neither.
APPLIED_DEV_CONTRACT = {
    "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
}



def test_storage_bucket_specs_dedup() -> None:
    cfg = om.OpenMetadataConfig.from_environment(
        {
            **APPLIED_DEV_CONTRACT,
            "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": "{}",
            "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
        },
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root="lakehouse_code",
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    names = [spec["name"] for spec in deployer.storage_bucket_specs()]
    assert names == ["lakehouse-bronze", "lakehouse-silver", "lakehouse-gold"]


def test_deployment_input_validation_rejects_new_product_without_contract() -> None:
    cfg = om.OpenMetadataConfig.from_environment(
        {
            **APPLIED_DEV_CONTRACT,
            "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": "{}",
            "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
        },
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root="lakehouse_code",
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    product = governance.ProductSpec(
        id="new_product", display_name="New Product", description="", domain="sales", gold_tables=()
    )
    domains = [
        governance.DomainSpec(
            name="sales",
            display_name="Sales",
            description="",
            silver_tables=(),
            bronze=(),
            products=(product,),
        )
    ]

    with pytest.raises(om.OpenMetadataError, match="not covered by the provider contract"):
        deployer.validate_deployment_inputs(domains)


def test_domain_specs_validate_composite_inventory_with_provider_schema_fqns(tmp_path: Path) -> None:
    (tmp_path / "bronze" / "crm").mkdir(parents=True)
    (tmp_path / "bronze" / "crm" / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1beta1
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: active
resources:
  - name: customers
"""
    )
    (tmp_path / "governance.yml").write_text(
        """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    products:
      - id: customer_health
        displayName: Customer Health
        description: Customer health product.
"""
    )
    write_floe_entity(tmp_path, "sales", "accounts", "crm")
    cfg = om.OpenMetadataConfig.from_environment(
        {
            **APPLIED_DEV_CONTRACT,
            "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": (
                '{"sales": "polaris.lakehouse_dev.sales_silver"}'
            ),
            "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": (
                '{"customer_health": "polaris.lakehouse_dev.customer_health_gold"}'
            ),
        },
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root=str(tmp_path),
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )

    with pytest.raises(
        om.OpenMetadataError,
        match="Silver entity sales/accounts reads unknown Bronze resource crm/accounts",
    ):
        om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))._domain_specs()


def test_deploy_seeds_medallion_buckets_and_bronze_source_hierarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    write_floe_entity(tmp_path, "sales", "orders", "crm")
    cfg = om.OpenMetadataConfig.from_environment(
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
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    seeded_containers = []

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)

    fake = FakeOpenMetadata()
    fake.seed("tables", "polaris.lakehouse_dev.order_revenue_gold.crawled", {})
    monkeypatch.setattr(deployer.client, "request", fake.request)

    def record_container(name, parent_fqn, full_path, description) -> None:
        seeded_containers.append((name, parent_fqn, full_path, description))

    monkeypatch.setattr(deployer, "ensure_container", record_container)

    deployer.deploy()

    assert [(name, parent_fqn, full_path) for name, parent_fqn, full_path, _ in seeded_containers] == [
        ("lakehouse-bronze", None, "s3://lakehouse-bronze"),
        ("lakehouse-silver", None, "s3://lakehouse-silver"),
        ("lakehouse-gold", None, "s3://lakehouse-gold"),
        ("crm", "seaweedfs.lakehouse-bronze", "s3://lakehouse-bronze/crm"),
        (
            "orders",
            "seaweedfs.lakehouse-bronze.crm",
            "s3://lakehouse-bronze/crm/orders",
        ),
    ]
    assert seeded_containers[-1][3] == "Raw sales orders."


def _single_product_deployer(
    tmp_path: Path, *, environ: dict[str, str] | None = None, catalog_database: str = "lakehouse_dev"
) -> om.OpenMetadataDeployer:
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
    write_floe_entity(tmp_path, "sales", "orders", "crm")
    models = tmp_path / "gold" / "models" / "order_revenue"
    models.mkdir(parents=True)
    (models / "mart_revenue_by_day.sql").write_text("select 1\n")
    cfg = om.OpenMetadataConfig.from_environment(
        {**APPLIED_DEV_CONTRACT, **(environ or {})},
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root=str(tmp_path),
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database=catalog_database,
    )
    return om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))


def test_deploy_does_not_touch_the_unscoped_legacy_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _single_product_deployer(tmp_path)

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "_domain_specs", lambda: [])
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "storage_bucket_specs", lambda: [])
    fake = FakeOpenMetadata()
    monkeypatch.setattr(deployer.client, "request", fake.request)

    deployer.deploy()

    # The marker property is defined on the entity types; no entity is written.
    assert [call for call in fake.writes if "/metadata/types/" not in call[1]] == []


DEV = "polaris.lakehouse_dev"


def test_deploy_registers_every_table_a_pipeline_writes_before_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenMetadata 1.13.6 drops lineage for a table it does not hold yet, so
    each Silver (Floe entity) and Gold (dbt model) table is registered first,
    and an existing table is never overwritten."""
    deployer = _single_product_deployer(tmp_path)
    fake = FakeOpenMetadata()

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "ensure_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployer.client, "request", fake.request)

    deployer.deploy()

    posts = [call for call in fake.calls if call == ("POST", "/api/v1/tables")]
    assert len(posts) == 2
    assert set(fake.entities["tables"]) == {
        f"{DEV}.sales_silver.orders",
        f"{DEV}.order_revenue_gold.mart_revenue_by_day",
    }


def test_deploy_sets_the_domain_on_schemas_and_attaches_only_gold_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _single_product_deployer(tmp_path)
    fake = FakeOpenMetadata()

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "ensure_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployer.client, "request", fake.request)

    deployer.deploy()

    domain_id = fake.entities["domains"]["sales"]["id"]
    schemas = fake.entities["databaseSchemas"]
    assert {fqn: [ref["id"] for ref in schema["domains"]] for fqn, schema in schemas.items()} == {
        f"{DEV}.sales_silver": [domain_id],
        f"{DEV}.order_revenue_gold": [domain_id],
    }
    gold_table = fake.entities["tables"][f"{DEV}.order_revenue_gold.mart_revenue_by_day"]
    assert fake.entities["dataProducts"]["order_revenue"]["assets"] == [{"id": gold_table["id"], "type": "table"}]


def test_deploy_reports_an_empty_gold_schema_as_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer = _single_product_deployer(tmp_path)

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "ensure_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployer.client, "request", FakeOpenMetadata().request)
    monkeypatch.setattr(deployer, "list_schema_tables", lambda schema_fqn: [])

    with pytest.raises(om.OpenMetadataError, match="order_revenue_gold"):
        deployer.deploy()


def test_deploy_refuses_another_stages_schemas_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One OpenMetadata deployment holds every governed stage, so a deploy that
    resolved its schema FQNs from one stage's contract environment and its
    database from another's would seed the wrong stage's catalog."""
    deployer = _single_product_deployer(
        tmp_path,
        environ={
            # A PROD contract was applied, so this environment is attributable
            # -- but it still carries the database FQN and schema maps a DEV
            # run left behind, which agree with each other and must not be
            # what the guard trusts.
            "OPENLAKEFORGE_CONTRACT_STAGE": "prod",
            "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_prod",
            "OPENLAKEFORGE_CATALOG_DATABASE_FQN": "polaris.lakehouse_dev",
            "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": '{"sales": "polaris.lakehouse_dev.sales_silver"}',
            "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": (
                '{"order_revenue": "polaris.lakehouse_dev.order_revenue_gold"}'
            ),
        },
        catalog_database="lakehouse_prod",
    )
    requests: list[tuple[str, str]] = []

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    def request(method: str, path: str, **_kwargs):
        requests.append((method, path))
        return {"id": "t", "fullyQualifiedName": "t"}

    monkeypatch.setattr(deployer.client, "request", request)
    monkeypatch.setattr(deployer, "ensure_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)

    with pytest.raises(om.OpenMetadataError) as excinfo:
        deployer.deploy()

    assert "polaris.lakehouse_dev.sales_silver" in str(excinfo.value)
    assert "polaris.lakehouse_prod" in str(excinfo.value)
    assert requests == []


def test_missing_assets_guidance_is_stage_neutral_in_error_and_warning_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guidance names the empty Gold schema and tells the operator to rerun
    with their original options, without prescribing local-only flags."""
    deployer = _single_product_deployer(tmp_path)
    (domain,) = deployer._domain_specs()
    [product] = domain.products
    # Unsorted, with a repeated product: the message must list each missing
    # table FQN once, in sorted order.
    domain = dataclasses.replace(domain, products=(product, dataclasses.replace(product, id="churn"), product))
    monkeypatch.setattr(deployer, "_domain_specs", lambda: [domain])
    monkeypatch.setattr(
        deployer.config,
        "catalog_gold_schema_fqns",
        {**deployer.config.catalog_gold_schema_fqns, "churn": "polaris.lakehouse_dev.churn_gold"},
    )
    expected_lines = ["- polaris.lakehouse_dev.churn_gold", "- polaris.lakehouse_dev.order_revenue_gold"]
    warnings: list[str] = []

    class Progress:
        def info(self, message: str) -> None:
            pass

        def warn(self, message: str) -> None:
            warnings.append(message)

    monkeypatch.setattr(deployer, "progress", Progress())
    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "ensure_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployer.client, "request", FakeOpenMetadata().request)
    monkeypatch.setattr(deployer, "list_schema_tables", lambda schema_fqn: [])

    with pytest.raises(om.OpenMetadataError) as excinfo:
        deployer.deploy()
    message = str(excinfo.value)
    assert "--cluster-name" not in message
    assert "--kubeconfig-path" not in message
    assert [line.strip() for line in message.splitlines() if line.strip().startswith("- ")] == expected_lines
    assert "same deployment and provider options as the original run" in message

    monkeypatch.setattr(deployer.config, "allow_missing_assets", True)
    deployer.deploy()
    assert len(warnings) == 1
    assert [line.strip() for line in warnings[0].splitlines() if line.strip().startswith("- ")] == expected_lines
    assert "--cluster-name" not in warnings[0]
