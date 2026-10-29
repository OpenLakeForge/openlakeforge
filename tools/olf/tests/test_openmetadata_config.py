from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from olf import openmetadata as om
from olf.openmetadata._config import OpenMetadataConfig

# `build_contract_env` writes these only where a provider contract was
# actually applied for the stage being deployed; `OpenMetadataConfig` refuses
# an environment that carries neither.
APPLIED_DEV_CONTRACT = {
    "OPENLAKEFORGE_CONTRACT_STAGE": "dev",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
}



def _write_lakehouse(root: Path, name: str = "override", domain: str = "sales", product_id: str = "widgets") -> Path:
    """Write a canonical lakehouse layout: lakehouse.yaml plus one bronze source."""
    lakehouse_path = root / "lakehouse.yaml"
    source_path = root / "bronze" / "crm" / "source.yaml"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    lakehouse_path.write_text(
        f"""apiVersion: openlakeforge.io/v1alpha3
kind: Lakehouse
name: {name}
displayName: {name.title()}
description: {name} lakehouse.
status: planned
sources:
  - crm
domains:
  - name: {domain}
    displayName: {domain.title()}
    description: {domain} domain.
    status: planned
    silver_tables:
      tables:
        - name: source
          source: crm
          resource: source
    products:
      - id: {product_id}
        displayName: {product_id.replace('_', ' ').title()}
        description: {product_id} product.
        status: planned
        silver_inputs: [source]
        gold_tables:
          tables:
            - name: mart_{product_id}
dashboards:
  - name: {product_id}_dashboard
    products: [{product_id}]
""",
        encoding="utf-8",
    )
    source_path.write_text(
        """apiVersion: openlakeforge.io/v1alpha3
kind: Source
name: crm
displayName: CRM
description: CRM source.
status: planned
resources:
  - name: source
""",
        encoding="utf-8",
    )
    return lakehouse_path


def test_config_from_environment_reads_schema_fqns() -> None:
    environ = {
        **APPLIED_DEV_CONTRACT,
        "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": '{"order_revenue": "svc.db.order_revenue_silver"}',
        "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
        "OPENLAKEFORGE_STORAGE_OM_SERVICE": "aws_s3",
        "OPENLAKEFORGE_STORAGE_DISPLAY_NAME": "AWS S3",
        "OPENLAKEFORGE_STORAGE_ENDPOINT": "",
        "OPENLAKEFORGE_STORAGE_REGION": "eu-west-1",
        "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "openlakeforge-poc-bronze",
        "OPENLAKEFORGE_STORAGE_SILVER_BUCKET": "openlakeforge-poc-silver",
        "OPENLAKEFORGE_STORAGE_GOLD_BUCKET": "openlakeforge-poc-gold",
    }
    cfg = OpenMetadataConfig.from_environment(
        environ,
        base_url="http://127.0.0.1:18585/",
        admin_email="admin@open-metadata.org",
        admin_password="admin",
        metadata_root="domains",
        metadata_source_dir="",
        allow_missing_assets=True,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )
    assert cfg.base_url == "http://127.0.0.1:18585"
    assert cfg.catalog_database_fqn == "polaris.lakehouse_dev"
    assert cfg.catalog_silver_schema_fqns == {"order_revenue": "svc.db.order_revenue_silver"}
    assert cfg.storage_service == "aws_s3"
    assert cfg.storage_display_name == "AWS S3"
    assert cfg.storage_endpoint == ""
    assert cfg.storage_region == "eu-west-1"
    assert cfg.storage_bronze_bucket == "openlakeforge-poc-bronze"
    assert cfg.storage_silver_bucket == "openlakeforge-poc-silver"
    assert cfg.storage_gold_bucket == "openlakeforge-poc-gold"


def test_config_from_environment_rejects_a_non_string_schema_fqn_value() -> None:
    """A schema-FQN map value that isn't a string must fail closed at parse time,
    not surface as an AttributeError once `_reconciliation.py` calls `.startswith`
    on it (#218)."""
    environ = {
        **APPLIED_DEV_CONTRACT,
        "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": '{"order_revenue": null}',
        "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
    }
    with pytest.raises(
        om.OpenMetadataError,
        match="OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON.*'order_revenue'",
    ):
        OpenMetadataConfig.from_environment(
            environ,
            base_url="http://x",
            admin_email="a",
            admin_password="p",
            metadata_root="domains",
            metadata_source_dir="",
            allow_missing_assets=False,
            catalog_service="polaris",
            catalog_database="lakehouse_dev",
        )


def test_config_from_environment_defaults_seed_schema_fqns_for_direct_cli(tmp_path: Path) -> None:
    _write_lakehouse(tmp_path)

    cfg = OpenMetadataConfig.from_environment(
        dict(APPLIED_DEV_CONTRACT),
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root=str(tmp_path),
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="",
        catalog_database="",
    )

    assert cfg.catalog_service == "polaris"
    assert cfg.catalog_database == "lakehouse_dev"
    assert cfg.catalog_silver_schema_fqns == {"sales": "polaris.lakehouse_dev.sales_silver"}
    assert cfg.catalog_gold_schema_fqns == {"widgets": "polaris.lakehouse_dev.widgets_gold"}


def test_config_from_environment_derives_defaults_from_metadata_source_dir_override(tmp_path: Path) -> None:
    """metadata_source_dir must drive the FQN defaults, matching what domain_files() actually deploys.

    metadata_root here does not even exist, so the defaults could only have
    come from the override.
    """
    source_dir = tmp_path / "override"
    source_dir.mkdir()
    lakehouse_path = _write_lakehouse(source_dir)

    cfg = OpenMetadataConfig.from_environment(
        dict(APPLIED_DEV_CONTRACT),
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root=str(tmp_path / "does-not-exist"),
        metadata_source_dir=str(source_dir),
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )

    assert cfg.catalog_silver_schema_fqns == {"sales": "polaris.lakehouse_dev.sales_silver"}
    assert cfg.catalog_gold_schema_fqns == {"widgets": "polaris.lakehouse_dev.widgets_gold"}
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    assert deployer.domain_files() == [lakehouse_path, source_dir / "bronze" / "crm" / "source.yaml"]


def test_config_from_environment_accepts_a_standalone_lakehouse_directory_override(tmp_path: Path) -> None:
    """A directory override's parent directory name must not have to match the lakehouse.

    Mirrors a mounted or temporary path like /metadata/, where the parent
    directory carries no significance — only the descriptor's own `name:`
    field identifies the lakehouse. The override must carry the canonical
    bronze/*/source.yaml siblings so schema-FQN defaults stay derivable.
    """
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    lakehouse_path = _write_lakehouse(metadata_dir, name="sales")

    cfg = OpenMetadataConfig.from_environment(
        dict(APPLIED_DEV_CONTRACT),
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root=str(tmp_path / "does-not-exist"),
        metadata_source_dir=str(metadata_dir),
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )

    assert cfg.catalog_silver_schema_fqns == {"sales": "polaris.lakehouse_dev.sales_silver"}
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    assert deployer.domain_files() == [lakehouse_path, metadata_dir / "bronze" / "crm" / "source.yaml"]


def test_config_from_environment_accepts_a_standalone_lakehouse_file_override(tmp_path: Path) -> None:
    """A metadata_source_dir naming lakehouse.yaml directly must still resolve its sibling sources.

    Mirrors a mounted single-file override like /metadata/lakehouse.yaml. The
    schema-FQN defaults need the bronze/*/source.yaml descriptors next to it,
    not just the lakehouse.yaml itself.
    """
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    lakehouse_path = _write_lakehouse(metadata_dir, name="sales")

    cfg = OpenMetadataConfig.from_environment(
        dict(APPLIED_DEV_CONTRACT),
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root=str(tmp_path / "does-not-exist"),
        metadata_source_dir=str(lakehouse_path),
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_dev",
    )

    assert cfg.catalog_silver_schema_fqns == {"sales": "polaris.lakehouse_dev.sales_silver"}
    deployer = om.OpenMetadataDeployer(cfg, om.OpenMetadataClient(cfg.base_url))
    assert deployer.domain_files() == [lakehouse_path, metadata_dir / "bronze" / "crm" / "source.yaml"]


def test_config_from_environment_preserves_explicit_empty_schema_contract() -> None:
    cfg = OpenMetadataConfig.from_environment(
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

    assert cfg.catalog_silver_schema_fqns == {}
    assert cfg.catalog_gold_schema_fqns == {}


def test_openmetadata_command_anchors_its_default_metadata_root_to_the_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from olf import k8s, openmetadata
    from olf.commands import openmetadata as command

    project = tmp_path / "project"
    distribution = tmp_path / "distribution"
    captured: dict[str, str] = {}
    monkeypatch.setenv("OPENLAKEFORGE_PROJECT_ROOT", str(project))
    monkeypatch.setenv("OLF_DISTRIBUTION_ROOT", str(distribution))
    monkeypatch.setattr(k8s, "wait_for_rollout", lambda *_args: None)
    monkeypatch.setattr(k8s, "configmap_data", lambda *_args: None)

    @contextmanager
    def _port_forward(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        yield 18585

    monkeypatch.setattr(k8s, "port_forward", _port_forward)

    def _config(*_args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        captured["metadata_root"] = kwargs["metadata_root"]
        captured["admin_password"] = kwargs["admin_password"]
        return SimpleNamespace(base_url=kwargs["base_url"], catalog_service="polaris")

    monkeypatch.setattr(openmetadata.OpenMetadataConfig, "from_environment", staticmethod(_config))
    monkeypatch.setattr(openmetadata, "OpenMetadataClient", lambda _url, **_kwargs: object())
    monkeypatch.setattr("olf.openmetadata._auth.automation_token", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("olf.openmetadata._auth.admin_password", lambda *_args, **_kwargs: "generated")
    order: list[str] = []
    monkeypatch.setattr(
        openmetadata, "OpenMetadataDeployer", lambda *_args: SimpleNamespace(deploy=lambda: order.append("seed"))
    )
    monkeypatch.setattr(command, "run_trino_ingestion", lambda *_args, **_kwargs: order.append("ingest"))

    command.deploy_openmetadata_metadata()

    assert captured["metadata_root"] == str(project / "lakehouse_code")
    assert captured["admin_password"] == "generated"
    assert order == ["ingest", "seed"]


def test_openmetadata_command_reports_configuration_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from olf import k8s, openmetadata
    from olf.commands import openmetadata as command

    monkeypatch.setattr(k8s, "wait_for_rollout", lambda *_args: None)
    monkeypatch.setattr(k8s, "configmap_data", lambda *_args: None)

    @contextmanager
    def _port_forward(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        yield 18585

    def _invalid_config(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise openmetadata.OpenMetadataError("bad contract")

    monkeypatch.setattr(k8s, "port_forward", _port_forward)
    monkeypatch.setattr("olf.openmetadata._auth.admin_password", lambda *_args, **_kwargs: "generated")
    monkeypatch.setattr(openmetadata.OpenMetadataConfig, "from_environment", _invalid_config)

    with pytest.raises(typer.Exit) as exc_info:
        command.deploy_openmetadata_metadata()

    assert exc_info.value.exit_code == 1
    assert "ERROR: bad contract" in capsys.readouterr().err


def test_database_root_follows_the_selected_catalog_database_not_a_stale_fqn() -> None:
    """A whole contract environment carries over from stage to stage together,
    so a database FQN that agrees with the stale schema maps rather than with
    the stage being deployed must not define what this deploy may write."""
    cfg = OpenMetadataConfig.from_environment(
        {
            "OPENLAKEFORGE_CONTRACT_STAGE": "prod",
            "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_prod",
            "OPENLAKEFORGE_CATALOG_DATABASE_FQN": "polaris.lakehouse_dev",
            "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": '{"sales": "polaris.lakehouse_dev.sales_silver"}',
            "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
        },
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root="domains",
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database="lakehouse_prod",
    )

    assert cfg.catalog_database_fqn == "polaris.lakehouse_prod"


def _config_from(environ: dict[str, str], *, catalog_database: str) -> OpenMetadataConfig:
    return OpenMetadataConfig.from_environment(
        environ,
        base_url="http://x",
        admin_email="a",
        admin_password="p",
        metadata_root="domains",
        metadata_source_dir="",
        allow_missing_assets=False,
        catalog_service="polaris",
        catalog_database=catalog_database,
    )


def test_deploy_is_refused_when_no_contract_was_applied_for_this_environment() -> None:
    """The Terraform output being unreadable leaves every catalog value the
    calling shell carried in place, so a run intended for one stage resolves
    the stage whose environment it inherited. Nothing in those values says
    which; the absent provenance marker does."""
    stale_dev_environment = {
        "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
        "OPENLAKEFORGE_CATALOG_DATABASE_FQN": "polaris.lakehouse_dev",
        "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": '{"sales": "polaris.lakehouse_dev.sales_silver"}',
        "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
    }

    with pytest.raises(om.OpenMetadataError, match="No applied provider contract"):
        _config_from(stale_dev_environment, catalog_database="lakehouse_dev")


def test_deploy_is_refused_when_the_selected_database_is_not_the_applied_one() -> None:
    """OPENMETADATA_CATALOG_DATABASE is only defaulted from the contract, so a
    value inherited from an earlier stage outlives the contract that replaced
    it."""
    with pytest.raises(om.OpenMetadataError, match="is not an override"):
        _config_from(
            {
                "OPENLAKEFORGE_CONTRACT_STAGE": "prod",
                "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_prod",
                "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON": "{}",
                "OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON": "{}",
            },
            catalog_database="lakehouse_dev",
        )
