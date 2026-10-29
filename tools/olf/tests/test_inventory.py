from __future__ import annotations

from pathlib import Path

import pytest
from openlakeforge_domain import (
    LakehouseDescriptorError,
    inventory_for,
    load_lakehouse_inventory,
    load_lakehouse_inventory_from_descriptors,
)

ROOT = Path(__file__).parents[3]


def _lakehouse(product_id: str = "revenue", domain: str = "sales", source: str = "crm") -> str:
    return f"""\
apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - {source}
domains:
  - name: {domain}
    displayName: {domain.title()}
    description: {domain} domain.
    status: planned
    silver_tables:
      tables:
        - name: orders
          source: {source}
          resource: orders
    products:
      - id: {product_id}
        displayName: {product_id.replace('_', ' ').title()}
        description: {product_id} product.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_{product_id}
dashboards:
  - name: {product_id}_dashboard
    products: [{product_id}]
"""


def _source(source: str = "crm", resource: str = "orders") -> str:
    return f"""\
apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: {source}
displayName: {source.title()}
description: {source} source.
status: planned
resources:
  - name: {resource}
"""


def _write_lakehouse(root: Path, product_id: str = "revenue", domain: str = "sales", source: str = "crm") -> Path:
    lakehouse_dir = root / "lakehouse_code"
    lakehouse_dir.mkdir(parents=True, exist_ok=True)
    lakehouse_path = lakehouse_dir / "lakehouse.yaml"
    lakehouse_path.write_text(_lakehouse(product_id=product_id, domain=domain, source=source), encoding="utf-8")
    source_path = lakehouse_dir / "bronze" / source / "source.yaml"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(_source(source=source), encoding="utf-8")
    return lakehouse_path


def test_seed_inventory_derives_every_product_expectation() -> None:
    inventory = load_lakehouse_inventory(ROOT)

    assert [domain.name for domain in inventory.domains] == ["sales", "supply_chain"]
    assert [product.name for product in inventory.products] == [
        "order_revenue",
        "customer_health",
        "inventory_reliability",
    ]
    assert [product.job_name for product in inventory.products] == [
        "order_revenue_pipeline",
        "customer_health_pipeline",
        "inventory_reliability_pipeline",
    ]
    assert sorted(inventory.catalog_namespaces) == sorted(
        {
            "crm_bronze",
            "erp_bronze",
            "sales_silver",
            "supply_chain_silver",
            "order_revenue_gold",
            "customer_health_gold",
            "inventory_reliability_gold",
        }
    )
    assert inventory.products[0].openmetadata_data_product_fqns == (
        "order_revenue",
        "sales.order_revenue",
    )
    assert inventory.domains[0].artifact_prefixes.floe_report_prefix == "floe/reports/sales/"
    assert inventory.domain_names == ("sales", "supply_chain")
    assert [entity.name for entity in inventory.floe_entities(inventory.domains[1])] == [
        "inventory_snapshots",
        "purchase_orders",
        "shipments",
        "stockout_events",
        "suppliers",
        "warehouses",
    ]
    assert {(entity.source, entity.resource) for entity in inventory.floe_entities(inventory.domains[0])} <= {
        ("crm", resource.name) for resource in inventory.sources[0].resources
    }
    assert inventory.openmetadata_data_products == {
        "order_revenue": ("order_revenue", "sales.order_revenue"),
        "customer_health": ("customer_health", "sales.customer_health"),
        "inventory_reliability": ("inventory_reliability", "supply_chain.inventory_reliability"),
    }
    assert inventory.silver_namespace_names == frozenset({"sales_silver", "supply_chain_silver"})
    assert inventory.gold_namespace_names == frozenset(
        {"order_revenue_gold", "customer_health_gold", "inventory_reliability_gold"}
    )
    assert inventory.dashboards[0].report_source_dir == "lakehouse_code/dashboards/superset/sales_customer_health"


def test_governance_yml_yields_the_inventory_lakehouse_yaml_does() -> None:
    """The reference governance.yml names the same domains, products and dashboards as lakehouse.yaml."""
    code = ROOT / "lakehouse_code"
    legacy = load_lakehouse_inventory_from_descriptors(
        code / "lakehouse.yaml", sorted(code.glob("bronze/*/source.yaml")), source_label=code
    )
    current = load_lakehouse_inventory(ROOT)

    assert [(d.name, d.display_name, d.products) for d in current.domains] == [
        (d.name, d.display_name, d.products) for d in legacy.domains
    ]
    assert sorted(current.dashboards, key=lambda d: d.name) == sorted(legacy.dashboards, key=lambda d: d.name)
    assert current.source_names == legacy.source_names


def test_governance_yml_alone_names_the_inventory(tmp_path: Path) -> None:
    code = tmp_path / "lakehouse_code"
    (code / "bronze" / "crm").mkdir(parents=True)
    (code / "bronze" / "crm" / "source.yaml").write_text(_source(), encoding="utf-8")
    (code / "governance.yml").write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: Governance\n"
        "domains:\n  - {name: sales, displayName: Sales}\n"
        "products:\n  - {id: revenue, domain: sales, displayName: Revenue}\n",
        encoding="utf-8",
    )
    board = code / "dashboards" / "superset" / "revenue_board"
    (board / "datasets" / "Trino").mkdir(parents=True)
    (board / "metadata.yaml").write_text("type: assets\n", encoding="utf-8")
    (board / "datasets" / "Trino" / "mart.yml").write_text("schema: revenue_gold\n", encoding="utf-8")
    (code / "dashboards" / "superset" / "draft").mkdir()

    inventory = load_lakehouse_inventory(tmp_path)

    assert inventory.source_names == ("crm",)
    assert [(p.domain_name, p.id) for p in inventory.products] == [("sales", "revenue")]
    assert [(d.name, d.products) for d in inventory.dashboards] == [("revenue_board", ("revenue",))]


def test_governance_yml_wins_over_lakehouse_yaml(tmp_path: Path) -> None:
    _write_lakehouse(tmp_path, product_id="legacy")
    code = tmp_path / "lakehouse_code"
    (code / "governance.yml").write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: Governance\n"
        "domains:\n  - {name: sales, displayName: Sales}\n"
        "products:\n  - {id: current, domain: sales, displayName: Current}\n",
        encoding="utf-8",
    )

    assert [p.id for p in load_lakehouse_inventory(tmp_path).products] == ["current"]


def test_an_empty_project_without_governance_yml_loads_only_when_incomplete_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "lakehouse_code").mkdir()

    with pytest.raises(LakehouseDescriptorError, match="no governance.yml"):
        load_lakehouse_inventory(tmp_path)
    assert load_lakehouse_inventory(tmp_path, allow_incomplete=True).domains == ()


@pytest.mark.parametrize("content", ["not a mapping\n", "- unexpected\n"])
def test_a_malformed_legacy_descriptor_is_a_descriptor_error(tmp_path: Path, content: str) -> None:
    (tmp_path / "lakehouse_code").mkdir()
    (tmp_path / "lakehouse_code" / "lakehouse.yaml").write_text(content, encoding="utf-8")

    with pytest.raises(LakehouseDescriptorError):
        load_lakehouse_inventory(tmp_path, allow_incomplete=True)


def test_inventory_for_caches_by_resolved_repo_root() -> None:
    assert inventory_for(ROOT) is inventory_for(ROOT)
    assert inventory_for(ROOT) is inventory_for(str(ROOT))


def test_inventory_resolves_provider_physical_names() -> None:
    inventory = load_lakehouse_inventory(ROOT)
    names = inventory.resolve_physical_names(
        catalog_database_fqn="aws_glue.lakehouse_dev",
        bronze_bucket="openlakeforge-poc-bronze",
        silver_bucket="openlakeforge-poc-silver",
        gold_bucket="openlakeforge-poc-gold",
    )

    assert names.catalog_namespaces[0].name == "sales_silver"
    assert names.catalog_namespaces[0].location == "s3://openlakeforge-poc-silver/sales_silver/"
    assert names.silver_schema_fqns["sales"] == "aws_glue.lakehouse_dev.sales_silver"
    assert names.gold_schema_fqns["inventory_reliability"] == (
        "aws_glue.lakehouse_dev.inventory_reliability_gold"
    )
    assert names.bronze_namespaces["crm"] == "crm_bronze"
    assert names.bronze_schema_fqns["crm"] == "aws_glue.lakehouse_dev.crm_bronze"


def test_renaming_descriptor_product_changes_discovered_work_without_shared_code(tmp_path: Path) -> None:
    _write_lakehouse(tmp_path, product_id="revenue")

    inventory = load_lakehouse_inventory(tmp_path)
    product = inventory.default_product

    assert product.job_name == "revenue_pipeline"
    assert product.silver_namespace == "sales_silver"
    assert product.openmetadata_data_product_fqns == ("revenue", "sales.revenue")
    assert inventory.domains[0].artifact_prefixes.floe_report_prefix == "floe/reports/sales/"
    assert product.definitions_module == "lakehouse_code.pipelines.dagster.revenue"
    assert inventory.job_names == ("revenue_pipeline",)
    assert product.gold_namespace == "revenue_gold"
    assert inventory.silver_namespace_names == frozenset({"sales_silver"})


def test_a_floe_entity_reading_an_undeclared_bronze_resource_is_rejected(tmp_path: Path) -> None:
    _write_lakehouse(tmp_path)
    entity_dir = tmp_path / "lakehouse_code" / "silver" / "sales" / "contracts" / "floe"
    entity_dir.mkdir(parents=True)
    (entity_dir / "_domain.yml").write_text('name: "sales"\n', encoding="utf-8")
    (entity_dir / "orders.yml").write_text('name: "orders"\nsource: { ref: "crm", resource: "orders" }\n')
    (entity_dir / "refunds.yml").write_text('name: "refunds"\nsource: { ref: "crm", resource: "refunds" }\n')

    inventory = load_lakehouse_inventory(tmp_path)

    with pytest.raises(LakehouseDescriptorError, match=r"sales/refunds reads unknown Bronze resource crm/refunds"):
        inventory.floe_entities(inventory.domains[0])


def test_inventory_accepts_domain_with_no_products(tmp_path: Path) -> None:
    """A domain may be declared with an empty products array and validate on
    its own, before its first product lands -- as long as some other domain
    in the lakehouse still has at least one product (see the sibling test
    for the all-domains-empty case, which is rejected)."""
    lakehouse_dir = tmp_path / "lakehouse_code"
    lakehouse_dir.mkdir(parents=True)
    (lakehouse_dir / "lakehouse.yaml").write_text(
        """\
apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - workday
  - crm
domains:
  - name: hr
    displayName: Hr
    description: hr domain.
    status: planned
    silver_tables:
      tables:
        - name: employees
          source: workday
          resource: employees
    products: []
  - name: sales
    displayName: Sales
    description: sales domain.
    status: planned
    silver_tables:
      tables:
        - name: orders
          source: crm
          resource: orders
    products:
      - id: revenue
        displayName: Revenue
        description: revenue product.
        status: planned
        silver_inputs: [orders]
        gold_tables:
          tables:
            - name: mart_revenue
dashboards: []
""",
        encoding="utf-8",
    )
    workday_path = lakehouse_dir / "bronze" / "workday" / "source.yaml"
    workday_path.parent.mkdir(parents=True)
    workday_path.write_text(_source(source="workday", resource="employees"), encoding="utf-8")
    crm_path = lakehouse_dir / "bronze" / "crm" / "source.yaml"
    crm_path.parent.mkdir(parents=True)
    crm_path.write_text(_source(source="crm", resource="orders"), encoding="utf-8")

    inventory = load_lakehouse_inventory(tmp_path)

    hr = next(domain for domain in inventory.domains if domain.name == "hr")
    assert hr.products == ()
    assert hr.silver_namespace == "hr_silver"
    assert [product.id for product in inventory.products] == ["revenue"]


def test_inventory_rejects_a_lakehouse_with_no_products_anywhere(tmp_path: Path) -> None:
    """A domain may be product-less while it is being seeded, but the
    lakehouse as a whole must still have at least one product somewhere --
    downstream tooling (default_product, check-dbt.sh, e2e) requires one."""
    lakehouse_dir = tmp_path / "lakehouse_code"
    lakehouse_dir.mkdir(parents=True)
    (lakehouse_dir / "lakehouse.yaml").write_text(
        """\
apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: test
displayName: Test
description: Test lakehouse.
status: planned
sources:
  - workday
domains:
  - name: hr
    displayName: Hr
    description: hr domain.
    status: planned
    silver_tables:
      tables:
        - name: employees
          source: workday
          resource: employees
    products: []
dashboards: []
""",
        encoding="utf-8",
    )
    source_path = lakehouse_dir / "bronze" / "workday" / "source.yaml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_source(source="workday", resource="employees"), encoding="utf-8")

    with pytest.raises(LakehouseDescriptorError, match=r"must declare at least one product"):
        load_lakehouse_inventory(tmp_path)


def _write_source_runtime(root: Path, runtime: str) -> None:
    source_path = root / "lakehouse_code" / "bronze" / "crm" / "source.yaml"
    source_path.write_text(_source() + runtime, encoding="utf-8")


def test_a_source_declares_its_runtime_keys_and_derives_their_stage_bindings(tmp_path: Path) -> None:
    _write_lakehouse(tmp_path)
    _write_source_runtime(tmp_path, "runtime:\n  config: [base_url]\n  secrets: [api_token]\n")

    (source,) = load_lakehouse_inventory(tmp_path).sources

    assert source.runtime_config == ("base_url",)
    assert source.runtime_secrets == ("api_token",)
    assert source.config_map_name == "olf-source-crm"
    assert source.credentials_secret_name == "olf-source-crm-credentials"
    assert source.runtime_env_name("api_token") == "SOURCES__CRM__API_TOKEN"


def test_a_source_without_runtime_keys_needs_no_bindings(tmp_path: Path) -> None:
    _write_lakehouse(tmp_path)

    (source,) = load_lakehouse_inventory(tmp_path).sources

    assert source.runtime_config == () and source.runtime_secrets == ()


@pytest.mark.parametrize("name", ["crm_", "crm__api"])
def test_a_source_with_runtime_keys_needs_a_name_that_maps_to_valid_bindings(tmp_path: Path, name: str) -> None:
    """`crm_` would bind ConfigMap `olf-source-crm-`, which Kubernetes rejects."""
    _write_lakehouse(tmp_path, source=name)
    source_path = tmp_path / "lakehouse_code" / "bronze" / name / "source.yaml"
    source_path.write_text(_source(source=name) + "runtime:\n  config: [base_url]\n", encoding="utf-8")

    with pytest.raises(LakehouseDescriptorError, match="underscore-separated words"):
        load_lakehouse_inventory(tmp_path)


def test_binding_names_are_valid_kubernetes_names_for_an_underscored_source() -> None:
    from openlakeforge_domain.inventory import Source

    source = Source(descriptor_path=Path("source.yaml"), name="marketing_platform", display_name="M", resources=())

    assert source.config_map_name == "olf-source-marketing-platform"
    assert source.runtime_env_name("base_url") == "SOURCES__MARKETING_PLATFORM__BASE_URL"


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        ("runtime:\n  config: [base_url]\n  secrets: [base_url]\n", "listed twice"),
        ("runtime:\n  config: [Base-URL]\n", r"runtime.config\[0\] must match"),
        ("runtime:\n  values: {base_url: https://api.example}\n", "config and secrets keys only"),
        ("runtime:\n  secrets: api_token\n", "runtime.secrets must be an array"),
        # dlt reads `__` as a section break: `a__b` + `c` and `a` + `b__c` would share a variable.
        ("runtime:\n  secrets: [api__token]\n", "underscore-separated words"),
        ("runtime:\n  config: [base_url_]\n", "underscore-separated words"),
    ],
)
def test_a_source_runtime_block_names_keys_only(tmp_path: Path, runtime: str, message: str) -> None:
    _write_lakehouse(tmp_path)
    _write_source_runtime(tmp_path, runtime)

    with pytest.raises(LakehouseDescriptorError, match=message):
        load_lakehouse_inventory(tmp_path)
