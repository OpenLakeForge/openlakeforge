from pathlib import Path

import pytest
from conftest import write_floe_entity
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_services.governance import MetadataConfig, MetadataDeployer
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

GOLD = "polaris.lakehouse_dev.order_revenue_gold"


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def step(self, message: str) -> None:
        self.messages.append(("step", message))

    def info(self, message: str) -> None:
        self.messages.append(("info", message))

    def warn(self, message: str) -> None:
        self.messages.append(("warn", message))


def _descriptors(root: Path) -> list[Path]:
    """One domain, one product, one Bronze resource, written where any caller might keep them."""
    (root / "bronze" / "crm").mkdir(parents=True)
    source = root / "bronze" / "crm" / "source.yaml"
    source.write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: Source\nname: crm\ndisplayName: CRM\n"
        "description: crm source.\nstatus: active\nresources:\n  - name: orders\n    description: Raw sales orders.\n"
    )
    governance = root / "governance.yml"
    governance.write_text(
        "apiVersion: openlakeforge.io/v1beta2\nkind: Governance\n"
        "domains:\n  - name: sales\n    displayName: Sales\n    description: Sales domain\n"
        "    products:\n      - id: order_revenue\n        displayName: Sales Order Revenue\n"
        "        description: Revenue from orders.\n"
    )
    write_floe_entity(root, "sales", "orders", "crm")
    return [governance, source]


def _deployer(
    root: Path,
    *,
    allow_missing_assets: bool,
    progress: _Recorder,
    descriptor_paths: list[Path] | None = None,
    revision: str = "rev1",
    gold_root: str = GOLD,
) -> MetadataDeployer:
    config = MetadataConfig(
        base_url="http://openmetadata",
        admin_email="admin@example.org",
        admin_password="secret",
        allow_missing_assets=allow_missing_assets,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
        catalog_database_fqn="polaris.lakehouse_dev",
        catalog_silver_schema_fqns={"sales": "polaris.lakehouse_dev.sales_silver"},
        catalog_gold_schema_fqns={"order_revenue": gold_root},
        storage_service="seaweedfs",
        storage_display_name="SeaweedFS S3",
        storage_endpoint="http://seaweedfs-s3:8333",
        storage_region="us-east-1",
        storage_bronze_bucket="lakehouse-bronze",
        storage_silver_bucket="lakehouse-silver",
        storage_gold_bucket="lakehouse-gold",
    )
    # A token on the client means the deployer never logs in with the admin password.
    return MetadataDeployer(
        config,
        OpenMetadataClient(config.base_url, token="bot-token"),
        descriptor_paths=_descriptors(root) if descriptor_paths is None else descriptor_paths,
        rerun_hint="then run it again.",
        revision=revision,
        progress=progress,
    )


def _serve(
    deployer: MetadataDeployer, monkeypatch: pytest.MonkeyPatch, *, gold_tables: list[str]
) -> FakeOpenMetadata:
    fake = FakeOpenMetadata()
    for name in gold_tables:  # tables the Trino crawler found, with the columns it read
        fake.seed("tables", f"{GOLD}.{name}", {"columns": [{"name": "revenue", "description": "from dbt"}]})
    monkeypatch.setattr(deployer.client, "request", fake.request)
    return fake


def test_deployer_seeds_the_descriptors_it_was_given_and_reports_each_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = _Recorder()
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=progress)
    fake = _serve(deployer, monkeypatch, gold_tables=["order_revenue"])

    deployer.deploy()

    assert ("PUT", "/api/v1/dataProducts/order_revenue/assets/add") in fake.calls
    assert not [path for _, path in fake.calls if "login" in path]
    infos = [message for level, message in progress.messages if level == "info"]
    assert "Reconciled OpenMetadata domain: sales" in infos
    assert infos[-1] == "OpenMetadata data product order_revenue holds 1 asset(s); attached 1"


def test_deployer_ends_its_missing_tables_advice_with_the_callers_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder())
    _serve(deployer, monkeypatch, gold_tables=[])

    with pytest.raises(OpenMetadataError, match=f"{GOLD}\n.*then run it again\\.$"):
        deployer.deploy()


def test_deployer_warns_instead_when_missing_tables_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = _Recorder()
    deployer = _deployer(tmp_path, allow_missing_assets=True, progress=progress)
    _serve(deployer, monkeypatch, gold_tables=[])

    deployer.deploy()

    level, message = progress.messages[-1]
    assert level == "warn"
    assert GOLD in message and message.endswith("then run it again.")


def test_deployer_with_no_descriptors_refuses_before_any_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder(), descriptor_paths=[])
    fake = _serve(deployer, monkeypatch, gold_tables=[])

    with pytest.raises(OpenMetadataError, match="No OpenMetadata lakehouse descriptor to deploy"):
        deployer.deploy()
    assert fake.writes == []


def test_every_entity_a_deploy_creates_carries_the_marker_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=True, progress=_Recorder(), revision="abc123")
    fake = _serve(deployer, monkeypatch, gold_tables=[])

    deployer.deploy()

    managed = {
        collection: {fqn for fqn, entity in entities.items() if entity.get("extension") == {"olfManaged": "git:abc123"}}
        for collection, entities in fake.entities.items()
    }
    assert managed["domains"] == {"sales"}
    assert managed["dataProducts"] == {"order_revenue"}
    assert managed["databaseSchemas"] == {"polaris.lakehouse_dev.sales_silver", GOLD}
    assert managed["tables"] == {"polaris.lakehouse_dev.sales_silver.orders"}
    assert len(managed["containers"]) == 5
    assert all(
        any(prop["name"] == "olfManaged" for prop in fake.types[t]["customProperties"])
        for t in ("domain", "dataProduct", "databaseSchema", "table", "container")
    )


def test_a_second_deploy_of_the_same_revision_makes_no_write_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder())
    fake = _serve(deployer, monkeypatch, gold_tables=["order_revenue"])
    deployer.deploy()
    fake.calls.clear()
    second = _deployer(
        tmp_path, allow_missing_assets=False, progress=_Recorder(), descriptor_paths=deployer.domain_files()
    )
    monkeypatch.setattr(second.client, "request", fake.request)

    second.deploy()

    assert fake.writes == []


def test_a_new_revision_restamps_the_marker_and_patches_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder(), revision="rev1")
    fake = _serve(first, monkeypatch, gold_tables=["order_revenue"])
    first.deploy()
    fake.calls.clear()
    second = _deployer(
        tmp_path,
        allow_missing_assets=False,
        progress=_Recorder(),
        descriptor_paths=first.domain_files(),
        revision="rev2",
    )
    monkeypatch.setattr(second.client, "request", fake.request)

    second.deploy()

    assert {method for method, _ in fake.writes} == {"PATCH"}
    assert fake.entities["domains"]["sales"]["extension"] == {"olfManaged": "git:rev2"}


def test_a_deploy_corrects_an_owned_field_and_leaves_crawled_and_human_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder())
    fake = _serve(deployer, monkeypatch, gold_tables=["order_revenue"])
    deployer.deploy()
    fake.entities["dataProducts"]["order_revenue"].update(description="edited in the UI", tags=[{"tagFQN": "PII.None"}])
    fake.entities["tables"][f"{GOLD}.order_revenue"].update(tags=[{"tagFQN": "Tier.Tier1"}])
    fake.calls.clear()

    deployer.deploy()

    assert fake.writes == [("PATCH", f"/api/v1/dataProducts/{fake.entities['dataProducts']['order_revenue']['id']}")]
    product = fake.entities["dataProducts"]["order_revenue"]
    assert product["description"] == "Revenue from orders."
    assert product["tags"] == [{"tagFQN": "PII.None"}]
    table = fake.entities["tables"][f"{GOLD}.order_revenue"]
    assert table["columns"] == [{"name": "revenue", "description": "from dbt"}]
    assert table["tags"] == [{"tagFQN": "Tier.Tier1"}]


def test_a_wrong_stage_mapping_is_rejected_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer(
        tmp_path,
        allow_missing_assets=False,
        progress=_Recorder(),
        gold_root="polaris.lakehouse_prod.order_revenue_gold",
    )
    fake = _serve(deployer, monkeypatch, gold_tables=[])

    with pytest.raises(OpenMetadataError, match="belongs to another stage"):
        deployer.deploy()

    assert fake.writes == []


def _deployed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MetadataDeployer, FakeOpenMetadata]:
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder())
    fake = _serve(deployer, monkeypatch, gold_tables=["order_revenue"])
    fake.seed("databases", "polaris.lakehouse_dev", {})
    deployer.deploy()
    fake.calls.clear()
    return deployer, fake


def test_drift_is_empty_after_a_clean_deploy_and_reads_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)

    assert deployer.drift() == []
    assert fake.writes == []


def test_drift_reports_missing_changed_and_stale_per_entity_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)
    del fake.entities["tables"]["polaris.lakehouse_dev.sales_silver.orders"]
    fake.entities["dataProducts"]["order_revenue"]["description"] = "edited in the UI"
    fake.seed("dataProducts", "renamed_product", {"extension": {"olfManaged": "git:old"}})
    fake.seed("tables", f"{GOLD}.removed_model", {"extension": {"olfManaged": "git:old"}})
    fake.seed("tables", f"{GOLD}.crawled_only", {})

    report = {(item.kind, item.entity, item.fqn, item.stage) for item in deployer.drift()}

    assert report == {
        ("missing", "table", "polaris.lakehouse_dev.sales_silver.orders", "dev"),
        ("changed", "data product", "order_revenue", "shared"),
        ("stale", "data product", "renamed_product", "shared"),
        ("stale", "table", f"{GOLD}.removed_model", "dev"),
    }
    assert fake.writes == []


def test_drift_reports_a_reference_that_cannot_be_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)
    del fake.entities["domains"]["sales"]
    del fake.entities["databases"]["polaris.lakehouse_dev"]

    unresolved = {(item.entity, item.fqn) for item in deployer.drift() if item.kind == "unresolved"}

    assert unresolved == {
        ("data product", "order_revenue"),
        ("database schema", "polaris.lakehouse_dev.sales_silver"),
        ("database schema", GOLD),
    }
