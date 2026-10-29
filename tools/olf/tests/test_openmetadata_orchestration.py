from pathlib import Path

import pytest

from olf import openmetadata as om

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
        metadata_root="domains",
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
        metadata_root="domains",
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    domain_specs = [
        (
            Path("domains/sales/domain.yaml"),
            {
                "name": "sales",
                "data_products": [
                    {
                        "id": "new_product",
                        "name": "sales_new_product",
                    }
                ],
            },
        )
    ]

    with pytest.raises(om.OpenMetadataError, match="not covered by the provider contract"):
        deployer.validate_deployment_inputs(domain_specs)


def test_deployment_input_validation_rejects_malformed_bronze_before_writes() -> None:
    cfg = om.OpenMetadataConfig.from_environment(
        {
            **APPLIED_DEV_CONTRACT,
            "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": "{}",
            "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
        },
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root="domains",
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    domain_specs = [
        (
            Path("domains/sales/domain.yaml"),
            {
                "name": "sales",
                "data_products": [{"name": "sales_order_revenue", "bronze": ["raw_orders"]}],
            },
        )
    ]

    with pytest.raises(om.OpenMetadataError, match="Bronze entry at index 0 must be an object"):
        deployer.validate_deployment_inputs(domain_specs)


def test_domain_specs_validate_composite_inventory_with_provider_schema_fqns(tmp_path: Path) -> None:
    (tmp_path / "bronze" / "crm").mkdir(parents=True)
    (tmp_path / "bronze" / "crm" / "source.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: active
resources:
  - name: customers
"""
    )
    (tmp_path / "lakehouse.yaml").write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: active
sources: [crm]
domains:
  - name: sales
    displayName: Sales
    description: Sales domain.
    status: active
    silver_tables:
      tables:
        - name: accounts
          source: crm
          resource: accounts
    products:
      - id: customer_health
        displayName: Customer Health
        description: Customer health product.
        status: active
        silver_inputs: [accounts]
        gold_tables:
          tables:
            - name: mart_customer_health
dashboards: []
"""
    )
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
        match="Silver table 'accounts' references unknown resource 'accounts' of source 'crm'",
    ):
        om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))._domain_specs()


def test_deploy_seeds_medallion_buckets_and_bronze_source_hierarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        - name: raw_orders
          source: crm
          resource: orders
    products:
      - id: order_revenue
        displayName: Sales Order Revenue
        description: Revenue from orders.
        status: active
        silver_inputs: [raw_orders]
        gold_tables:
          tables:
            - name: mart_order_revenue
dashboards: []
"""
    )
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

    def request(method: str, path: str, **_kwargs):
        if method == "GET" and path.startswith("/api/v1/tables?"):
            return {"data": [{"id": "table-id"}]}
        return {}

    monkeypatch.setattr(deployer.client, "request", request)
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
        - name: raw_orders
          source: crm
          resource: orders
        - name: raw_order_lines
          source: crm
          resource: orders
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
    requests: list[tuple[str, str]] = []

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "_domain_specs", lambda: [])
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "storage_bucket_specs", lambda: [])
    monkeypatch.setattr(
        deployer.client,
        "request",
        lambda method, path, **_kwargs: requests.append((method, path)) or {"id": "legacy"},
    )

    deployer.deploy()

    assert requests == []


def test_deploy_sets_the_domain_on_schemas_and_attaches_only_gold_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _single_product_deployer(tmp_path)
    writes: list[tuple[str, str, object]] = []
    gold = "polaris.lakehouse_dev.order_revenue_gold"

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "ensure_container", lambda *args, **kwargs: None)

    def request(method: str, path: str, **kwargs):
        if method == "GET":
            assert path.startswith("/api/v1/tables?") and gold in path.replace("%2E", ".")
            return {"data": [{"id": "gold-table"}]}
        writes.append((method, path, kwargs.get("json_body")))
        return {}

    monkeypatch.setattr(deployer.client, "request", request)

    deployer.deploy()

    assert [(path, body.get("name"), body.get("domains")) for _, path, body in writes if "Schemas" in path] == [
        ("/api/v1/databaseSchemas", "sales_silver", ["sales"]),
        ("/api/v1/databaseSchemas", "order_revenue_gold", ["sales"]),
    ]
    assert not [path for _, path, _ in writes if path.startswith("/api/v1/tables")]
    assert writes[-1] == (
        "PUT",
        "/api/v1/dataProducts/order_revenue/assets/add",
        {"assets": [{"id": "gold-table", "type": "table"}], "dryRun": False},
    )


def test_deploy_reports_an_empty_gold_schema_as_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer = _single_product_deployer(tmp_path)

    monkeypatch.setattr(deployer, "wait_for_openmetadata", lambda: None)
    monkeypatch.setattr(deployer, "login", lambda: None)
    monkeypatch.setattr(deployer, "ensure_storage_service", lambda: None)
    monkeypatch.setattr(deployer, "ensure_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployer.client, "request", lambda *args, **kwargs: {})

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
