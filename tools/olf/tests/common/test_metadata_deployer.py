from dataclasses import replace
from pathlib import Path

import pytest
from conftest import write_floe_entity
from fake_openmetadata import FakeOpenMetadata
from openlakeforge_services.governance import MetadataConfig, MetadataDeployer
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError, OpenMetadataTransientError

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


def _serve(deployer: MetadataDeployer, monkeypatch: pytest.MonkeyPatch, *, gold_tables: list[str]) -> FakeOpenMetadata:
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


def test_a_wrong_stage_mapping_is_rejected_before_any_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_an_unreadable_asset_listing_costs_one_idempotent_attach_and_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = _Recorder()
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=progress)
    fake = _serve(deployer, monkeypatch, gold_tables=["order_revenue"])
    deployer.deploy()
    fake.calls.clear()

    def request(method: str, path: str, **kwargs):
        if method == "GET" and path.endswith("/assets"):
            return {"code": 404, "message": "not found"}
        return fake.request(method, path, **kwargs)

    monkeypatch.setattr(deployer.client, "request", request)

    deployer.deploy()

    assert fake.writes == [("PUT", "/api/v1/dataProducts/order_revenue/assets/add")]
    assert len(fake.entities["dataProducts"]["order_revenue"]["assets"]) == 1
    assert any(level == "warn" and "order_revenue" in message for level, message in progress.messages)


def test_held_assets_are_read_across_pages_of_the_assets_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder())
    fake = _serve(deployer, monkeypatch, gold_tables=[])
    assets = [{"id": f"t{i}", "type": "table"} for i in range(5)]
    product = fake.seed("dataProducts", "order_revenue", {"assets": assets})
    pages: list[dict] = []

    def request(method: str, path: str, **kwargs):
        if path.endswith("/assets") and kwargs["params"]["offset"] < 5:
            offset = kwargs["params"]["offset"]
            page = {"data": product["assets"][offset : offset + 2], "paging": {"total": 5}}
            pages.append(page)
            return page
        return fake.request(method, path, **kwargs)

    monkeypatch.setattr(deployer.client, "request", request)

    assert deployer._reconciler.held_asset_ids("order_revenue") == {f"t{i}" for i in range(5)}
    assert len(pages) == 3


def test_a_leftover_custom_storage_endpoint_is_removed_when_the_desired_endpoint_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=True, progress=_Recorder())
    deployer.config.storage_endpoint = ""
    fake = _serve(deployer, monkeypatch, gold_tables=[])
    service = fake.seed(
        "services/storageServices",
        "seaweedfs",
        {
            "displayName": "SeaweedFS S3",
            "connection": {"config": {"awsConfig": {"awsRegion": "us-east-1", "endPointURL": "http://old:8333"}}},
        },
    )

    deployer.ensure_storage_service()

    assert service["connection"]["config"]["awsConfig"] == {"awsRegion": "us-east-1"}


def test_the_marker_revision_follows_the_descriptor_source_not_the_metadata_root(tmp_path: Path) -> None:
    import subprocess

    from olf.infra.openmetadata import OpenMetadataConfig
    from olf.infra.openmetadata._orchestration import _git_revision

    source = tmp_path / "other-checkout"
    source.mkdir()
    _descriptors(source)
    git = ["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    config = OpenMetadataConfig.from_environment(
        {"OPENLAKEFORGE_CONTRACT_STAGE": "dev", "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev"},
        base_url="http://x",
        admin_email="",
        admin_password="",
        metadata_root=str(tmp_path / "not-a-checkout"),
        metadata_source_dir=str(source),
        allow_missing_assets=True,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )

    assert _git_revision(config) == head


@pytest.mark.parametrize(
    "stored",
    [
        {},
        {"connection": {}},
        {"connection": {"config": {"type": "S3"}}},
    ],
)
def test_a_storage_service_missing_part_of_its_connection_gets_the_whole_missing_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: dict
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=True, progress=_Recorder())
    fake = _serve(deployer, monkeypatch, gold_tables=[])
    service = fake.seed("services/storageServices", "seaweedfs", {"displayName": "SeaweedFS S3", **stored})

    deployer.ensure_storage_service()

    aws = service["connection"]["config"]["awsConfig"]
    assert aws == {"awsRegion": "us-east-1", "endPointURL": "http://seaweedfs-s3:8333"}


@pytest.mark.parametrize(
    "error", [OpenMetadataError("HTTP 404", status=404), OpenMetadataTransientError("HTTP 500", status=500)]
)
def test_a_failing_asset_listing_still_attaches_the_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder())
    fake = _serve(deployer, monkeypatch, gold_tables=["order_revenue"])

    def request(method: str, path: str, **kwargs):
        if method == "GET" and path.endswith("/assets"):
            raise error
        return fake.request(method, path, **kwargs)

    monkeypatch.setattr(deployer.client, "request", request)

    deployer.deploy()

    assert len(fake.entities["dataProducts"]["order_revenue"]["assets"]) == 1


def test_another_stages_managed_containers_are_not_stale_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)
    marked = {"extension": {"olfManaged": "git:old"}}
    fake.seed("containers", "seaweedfs.lakehouse-prod-bronze", marked)
    fake.seed("containers", "seaweedfs.lakehouse-bronze.removed_source", marked)

    stale = {item.fqn for item in deployer.drift() if item.kind == "stale"}

    assert stale == {"seaweedfs.lakehouse-bronze.removed_source"}


def test_a_product_moved_to_another_domain_is_reported_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)
    other = fake.seed("domains", "finance", {})
    fake.entities["dataProducts"]["order_revenue"]["domains"] = [{"id": other["id"], "type": "domain"}]

    changed = [(item.entity, item.fqn, item.detail) for item in deployer.drift() if item.kind == "changed"]

    assert changed == [("data product", "order_revenue", "domains")]
    assert fake.writes == []


def test_a_gold_table_a_product_no_longer_holds_is_reported_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)
    fake.entities["dataProducts"]["order_revenue"]["assets"] = []

    changed = [(item.entity, item.fqn, item.detail) for item in deployer.drift() if item.kind == "changed"]

    assert changed == [("data product", "order_revenue", "assets: 1 table(s) of its Gold schema are not attached")]
    assert fake.writes == []


def test_marked_tables_of_a_schema_removed_from_git_are_reported_stale_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)
    marked = {"extension": {"olfManaged": "git:old"}}
    fake.seed("databaseSchemas", "polaris.lakehouse_dev.removed_gold", marked)
    fake.seed("tables", "polaris.lakehouse_dev.removed_gold.mart", marked)
    fake.seed("tables", "polaris.lakehouse_dev.removed_gold.crawled_only", {})

    stale = {(item.entity, item.fqn) for item in deployer.drift() if item.kind == "stale"}

    assert stale == {
        ("database schema", "polaris.lakehouse_dev.removed_gold"),
        ("table", "polaris.lakehouse_dev.removed_gold.mart"),
    }


def test_drift_reports_an_unreadable_asset_listing_instead_of_reading_it_as_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployer, fake = _deployed(tmp_path, monkeypatch)
    served = deployer.client.request

    def request(method: str, path: str, **kwargs):
        if path.endswith("/assets"):
            raise OpenMetadataError("403 forbidden")
        return served(method, path, **kwargs)

    monkeypatch.setattr(deployer.client, "request", request)

    unresolved = [item for item in deployer.drift() if item.kind == "unresolved"]

    assert [(item.entity, item.fqn) for item in unresolved] == [("data product", "order_revenue")]
    assert "could not be read" in unresolved[0].detail
    assert fake.writes == []


def test_the_stamped_revision_is_read_from_the_stage_not_derived_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olf.infra.openmetadata._drift import _stamped_revision

    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=_Recorder())
    fake = _serve(deployer, monkeypatch, gold_tables=["order_revenue"])
    assert _stamped_revision(deployer.client) is None

    fake.seed("domains", "sales", {"extension": {"olfManaged": "git:unknown"}})
    assert _stamped_revision(deployer.client) == ""
    fake.entities["domains"]["sales"]["extension"]["olfManaged"] = "git:abc123"
    assert _stamped_revision(deployer.client) == "abc123"


def test_a_dotted_bucket_name_matches_its_quoted_container_fqn(tmp_path: Path) -> None:
    from openlakeforge_services.drift import _in_stage_buckets

    config = _deployer(tmp_path, allow_missing_assets=True, progress=_Recorder()).config
    config = replace(config, storage_bronze_bucket="lakehouse.dev.bronze")

    assert _in_stage_buckets(config, 'seaweedfs."lakehouse.dev.bronze".removed_source')
    assert _in_stage_buckets(config, 'seaweedfs."lakehouse.dev.bronze"')
    assert not _in_stage_buckets(config, 'seaweedfs."lakehouse.dev.bronze2".other')
