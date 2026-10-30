from pathlib import Path

import pytest
from conftest import write_floe_entity
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
        "apiVersion: openlakeforge.io/v1alpha3\nkind: Source\nname: crm\ndisplayName: CRM\n"
        "description: crm source.\nstatus: active\nresources:\n  - name: orders\n    description: Raw sales orders.\n"
    )
    governance = root / "governance.yml"
    governance.write_text(
        "apiVersion: openlakeforge.io/v1beta1\nkind: Governance\n"
        "domains:\n  - name: sales\n    displayName: Sales\n    description: Sales domain\n    status: active\n"
        "products:\n  - id: order_revenue\n    domain: sales\n    displayName: Sales Order Revenue\n"
        "    description: Revenue from orders.\n    status: active\n"
    )
    write_floe_entity(root, "sales", "orders", "crm")
    return [governance, source]


def _deployer(
    root: Path, *, allow_missing_assets: bool, progress: _Recorder, descriptor_paths: list[Path] | None = None
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
        catalog_gold_schema_fqns={"order_revenue": GOLD},
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
        progress=progress,
    )


def _serve(
    deployer: MetadataDeployer, monkeypatch: pytest.MonkeyPatch, *, gold_tables: list[dict]
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def request(method: str, path: str, **_kwargs: object) -> dict:
        calls.append((method, path))
        if path.startswith("/api/v1/tables?"):
            return {"data": gold_tables}
        if path.startswith("/api/v1/containers/name/"):
            return {"id": "parent"}
        return {}

    monkeypatch.setattr(deployer.client, "request", request)
    return calls


def test_deployer_seeds_the_descriptors_it_was_given_and_reports_each_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = _Recorder()
    deployer = _deployer(tmp_path, allow_missing_assets=False, progress=progress)
    calls = _serve(deployer, monkeypatch, gold_tables=[{"id": "gold-table"}])

    deployer.deploy()

    assert ("PUT", "/api/v1/dataProducts/order_revenue/assets/add") in calls
    assert not [path for _, path in calls if "login" in path]
    infos = [message for level, message in progress.messages if level == "info"]
    assert "Upserted OpenMetadata domain: sales" in infos
    assert "Upserted OpenMetadata container: s3://lakehouse-bronze/crm/orders" in infos
    assert infos[-1] == "Attached 1 OpenMetadata asset(s) to data product: order_revenue"


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
    calls = _serve(deployer, monkeypatch, gold_tables=[])

    with pytest.raises(OpenMetadataError, match="No OpenMetadata lakehouse descriptor to deploy"):
        deployer.deploy()
    assert [call for call in calls if call[0] != "GET"] == []
