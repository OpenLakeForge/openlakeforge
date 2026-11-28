"""What a deploy publishes of the product graph: `consumesFrom` edges and Gold role tags, and what drift reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_core import scan_gold_roles
from openlakeforge_services.governance import MetadataConfig, MetadataDeployer
from test_metadata_deployer import _descriptors, _Recorder

from olf.common import contracts_check

GOVERNANCE = """\
apiVersion: openlakeforge.io/v1beta2
kind: Governance
domains:
  - name: sales
    displayName: Sales
    description: Sales domain
    products:
      - id: sales_core
        displayName: Sales Core
        description: Conformed facts and dimensions.
      - id: order_revenue
        displayName: Sales Order Revenue
        description: Revenue marts.
"""
CORE = "polaris.lakehouse_dev.sales_core_gold"
REVENUE = "polaris.lakehouse_dev.order_revenue_gold"
MODELS = {
    "sales_core": {"fct_order_lines": "select 1", "dim_account": "select 1"},
    "order_revenue": {"mart_revenue": "select * from {{ ref('fct_order_lines') }}"},
}


def _project(root: Path, models: dict[str, dict[str, str]] = MODELS) -> list[Path]:
    paths = _descriptors(root)
    paths[0].write_text(GOVERNANCE)
    for product, files in models.items():
        folder = root / "gold" / "models" / product
        folder.mkdir(parents=True, exist_ok=True)
        for name, sql in files.items():
            (folder / f"{name}.sql").write_text(sql)
        (folder / "schema.yml").write_text("version: 2\nmodels:\n  - name: fct_order_lines\n    access: public\n")
    return paths


def _deployer(root: Path, fake: FakeOpenMetadata, monkeypatch: pytest.MonkeyPatch, **kwargs) -> MetadataDeployer:
    config = MetadataConfig(
        base_url="http://openmetadata",
        admin_email="admin@example.org",
        admin_password="secret",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
        catalog_database_fqn="polaris.lakehouse_dev",
        catalog_silver_schema_fqns={"sales": "polaris.lakehouse_dev.sales_silver"},
        catalog_gold_schema_fqns={"sales_core": CORE, "order_revenue": REVENUE},
        storage_service="seaweedfs",
        storage_display_name="SeaweedFS S3",
        storage_endpoint="http://seaweedfs-s3:8333",
        storage_region="us-east-1",
        storage_bronze_bucket="lakehouse-bronze",
        storage_silver_bucket="lakehouse-silver",
        storage_gold_bucket="lakehouse-gold",
    )
    from openlakeforge_services.openmetadata import OpenMetadataClient

    deployer = MetadataDeployer(
        config,
        OpenMetadataClient(config.base_url, token="bot-token"),
        descriptor_paths=_project(root),
        revision="rev1",
        progress=_Recorder(),
        **kwargs,
    )
    monkeypatch.setattr(deployer.client, "request", fake.request)
    return deployer


def _crawled() -> FakeOpenMetadata:
    fake = FakeOpenMetadata()
    fake.seed("databases", "polaris.lakehouse_dev", {})
    for schema, models in ((CORE, MODELS["sales_core"]), (REVENUE, MODELS["order_revenue"])):
        for name in models:
            fake.seed("tables", f"{schema}.{name}", {"columns": []})
    return fake


def _tags(fake: FakeOpenMetadata, fqn: str) -> set[str]:
    return {tag["tagFQN"] for tag in fake.entities["tables"][fqn].get("tags", [])}


def _consumed(fake: FakeOpenMetadata, product: str) -> list[str]:
    return [ref["id"] for ref in fake.entities["dataProducts"][product].get("consumesFrom", [])]


def test_a_deploy_publishes_the_consumes_from_edge_and_the_role_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _crawled()

    _deployer(tmp_path, fake, monkeypatch).deploy()

    assert _consumed(fake, "order_revenue") == [fake.entities["dataProducts"]["sales_core"]["id"]]
    assert _consumed(fake, "sales_core") == []
    assert _tags(fake, f"{CORE}.fct_order_lines") == {"GoldRole.Fact"}
    assert _tags(fake, f"{CORE}.dim_account") == {"GoldRole.Dimension"}
    assert _tags(fake, f"{REVENUE}.mart_revenue") == {"GoldRole.Mart"}
    assert fake.entities["classifications"]["GoldRole"]["mutuallyExclusive"] is True
    assert "providesTo" not in fake.entities["dataProducts"]["sales_core"]


def test_a_product_holds_its_own_gold_tables_and_not_the_ones_it_consumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _crawled()

    _deployer(tmp_path, fake, monkeypatch).deploy()

    held = {ref["id"] for ref in fake.entities["dataProducts"]["order_revenue"]["assets"]}
    assert held == {fake.entities["tables"][f"{REVENUE}.mart_revenue"]["id"]}
    core = {ref["id"] for ref in fake.entities["dataProducts"]["sales_core"]["assets"]}
    assert core == {fake.entities["tables"][f"{CORE}.{name}"]["id"] for name in MODELS["sales_core"]}


def test_a_repeat_deploy_writes_nothing_and_leaves_an_empty_drift_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _crawled()
    _deployer(tmp_path / "first", fake, monkeypatch).deploy()
    fake.calls.clear()

    again = _deployer(tmp_path / "second", fake, monkeypatch)
    again.deploy()
    drift = again.drift()

    assert fake.writes == []
    assert drift == []


def test_a_person_s_tag_and_edge_survive_and_a_wrong_role_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _crawled()
    other = fake.seed("dataProducts", "other_product", {})
    wrong = {"tagFQN": "GoldRole.Mart", "source": "Classification", "labelType": "Manual", "state": "Confirmed"}
    pii = {"tagFQN": "PII.Sensitive", "source": "Classification", "labelType": "Manual", "state": "Confirmed"}
    fake.entities["tables"][f"{CORE}.dim_account"]["tags"] = [wrong, pii]
    fake.seed("dataProducts", "order_revenue", {"consumesFrom": [{"id": other["id"], "type": "dataProduct"}]})

    _deployer(tmp_path, fake, monkeypatch).deploy()

    assert _tags(fake, f"{CORE}.dim_account") == {"GoldRole.Dimension", "PII.Sensitive"}
    assert set(_consumed(fake, "order_revenue")) == {other["id"], fake.entities["dataProducts"]["sales_core"]["id"]}


def test_drift_reports_a_missing_edge_a_dropped_dependency_and_a_missing_role_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _crawled()
    deployer = _deployer(tmp_path, fake, monkeypatch)
    deployer.deploy()
    fake.entities["dataProducts"]["order_revenue"]["consumesFrom"] = []
    fake.entities["dataProducts"]["sales_core"]["consumesFrom"] = [
        {"id": fake.entities["dataProducts"]["order_revenue"]["id"], "type": "dataProduct", "name": "order_revenue"}
    ]
    del fake.entities["tables"][f"{CORE}.fct_order_lines"]["tags"]
    fake.calls.clear()

    details = {(item.fqn, item.detail) for item in deployer.drift()}

    assert fake.writes == []
    assert ("order_revenue", "consumesFrom: sales_core not attached") in details
    assert ("sales_core", "consumesFrom: order_revenue is held but no longer declared") in details
    assert (f"{CORE}.fct_order_lines", "tags (missing GoldRole.Fact)") in details
    assert len(details) == 3


def test_a_model_takes_its_role_from_the_prefix_or_from_its_declared_meta(tmp_path: Path) -> None:
    folder = tmp_path / "models" / "p"
    folder.mkdir(parents=True)
    for name in ("fct_a", "dim_b", "mart_c", "wide_d", "wide_e", "wide_f"):
        (folder / f"{name}.sql").write_text("select 1")
    (folder / "schema.yml").write_text(
        "version: 2\nmodels:\n"
        "  - name: wide_d\n    meta: {openlakeforge: {role: fact}}\n"
        "  - name: wide_e\n    config: {meta: {openlakeforge: {role: Dimension}}}\n"
        "  - name: wide_f\n    meta: {openlakeforge: {role: staging}}\n"
        "  - name: mart_c\n    meta: {openlakeforge: {role: dimension}}\n"
    )

    roles, problems = scan_gold_roles(tmp_path, ["p"])

    assert roles["p"] == {
        "fct_a": "Fact",
        "dim_b": "Dimension",
        "mart_c": "Dimension",
        "wide_d": "Fact",
        "wide_e": "Dimension",
    }
    assert len(problems) == 1 and "wide_f" in problems[0] and "'staging'" in problems[0]


def test_a_gold_model_without_a_role_fails_the_contracts_check(tmp_path: Path) -> None:
    _project(tmp_path / "lakehouse_code", {"sales_core": {"fct_order_lines": "select 1", "orders": "select 1"}})

    result = contracts_check._check_gold_roles(tmp_path)  # noqa: SLF001

    assert not result.ok
    assert "sales_core.orders: no role" in result.detail


def _graph_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from openlakeforge_core import load_lakehouse_inventory

    fake = _crawled()
    _deployer(tmp_path, fake, monkeypatch).deploy()
    inventory = load_lakehouse_inventory(tmp_path)
    tables = {
        "sales_core": [f"sales_core_gold.{n}" for n in MODELS["sales_core"]],
        "order_revenue": ["order_revenue_gold.mart_revenue"],
    }
    return fake, inventory, tables


def test_the_live_check_passes_on_a_deployed_graph_and_names_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.infra.e2e._assertions import assert_openmetadata_product_graph
    from olf.infra.e2e._shell import E2EError

    fake, inventory, tables = _graph_fixture(tmp_path, monkeypatch)
    # The fake serves entities by name with the same shape the live check reads.
    from openlakeforge_services.openmetadata import OpenMetadataClient

    client = OpenMetadataClient("http://openmetadata", token="t")
    monkeypatch.setattr(client, "request", fake.request)
    kwargs = {"database_fqn": "polaris.lakehouse_dev", "tables": tables}

    assert_openmetadata_product_graph(client, inventory, **kwargs)

    fake.entities["dataProducts"]["order_revenue"]["consumesFrom"] = []
    del fake.entities["tables"][f"{CORE}.dim_account"]["tags"]
    with pytest.raises(E2EError) as raised:
        assert_openmetadata_product_graph(client, inventory, **kwargs)
    assert "order_revenue does not consume from sales_core" in str(raised.value)
    assert f"{CORE}.dim_account lacks GoldRole.Dimension" in str(raised.value)


def test_a_model_without_a_role_is_deployed_untagged_with_a_warning_and_drift_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _crawled()
    deployer = _deployer(tmp_path, fake, monkeypatch)
    (tmp_path / "gold" / "models" / "sales_core" / "orders.sql").write_text("select 1")
    fake.seed("tables", f"{CORE}.orders", {"columns": []})

    deployer.deploy()
    unresolved = [item for item in deployer.drift() if item.kind == "unresolved"]

    assert not _tags(fake, f"{CORE}.orders")
    warnings = [message for level, message in deployer.progress.messages if level == "warn"]
    assert any("deployed untagged: sales_core.orders: no role" in message for message in warnings)
    assert [(item.entity, item.fqn) for item in unresolved] == [
        (
            "gold model",
            "sales_core.orders: no role; name it fct_, dim_ or mart_, "
            "or set meta.openlakeforge.role in its schema.yml entry",
        )
    ]


def test_the_asset_listing_is_paged_by_rows_even_when_a_page_repeats_a_row() -> None:
    from olf.infra.e2e._assertions import _attached_asset_ids

    rows = [{"id": "a"}, {"id": "b"}, {"id": "a"}]  # the listing shifted: "a" came back twice
    offsets: list[int] = []

    class Client:
        def request(self, _method: str, _path: str, *, params: dict) -> dict:
            offsets.append(params["offset"])
            return {"data": rows[params["offset"] : params["offset"] + 2], "paging": {"total": len(rows)}}

    assert _attached_asset_ids(Client(), "p") == {"a", "b"}  # type: ignore[arg-type]
    assert offsets == [0, 2]
