import json
from pathlib import Path

import pytest

from olf.contracts import build_contract_env, render_shell_exports

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_defaults_without_contracts_match_local_profile() -> None:
    exports, unsets = build_contract_env({}, None, repo_root=REPO_ROOT)
    assert exports["OPENLAKEFORGE_STORAGE_IMPLEMENTATION"] == "storage.s3_compatible.seaweedfs"
    assert exports["OPENLAKEFORGE_STORAGE_ENDPOINT"] == "http://seaweedfs-s3.olf-system:8333"
    assert exports["OPENLAKEFORGE_CATALOG_PROVIDER"] == "polaris"
    assert exports["OPENLAKEFORGE_CATALOG_WAREHOUSE"] == "lakehouse_dev"
    assert exports["OPENLAKEFORGE_ARTIFACT_BASE_URI"] == "s3://openlakeforge-ops"
    assert exports["OPENLAKEFORGE_CATALOG_DATABASE_FQN"] == "polaris.lakehouse_dev"
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON"]) == {
        "sales": "polaris.lakehouse_dev.sales_silver",
        "supply_chain": "polaris.lakehouse_dev.supply_chain_silver",
    }
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON"]) == {
        "order_revenue": "polaris.lakehouse_dev.order_revenue_gold",
        "customer_health": "polaris.lakehouse_dev.customer_health_gold",
        "inventory_reliability": "polaris.lakehouse_dev.inventory_reliability_gold",
    }
    assert exports["OPENMETADATA_CATALOG_SERVICE"] == "polaris"
    assert exports["OPENLAKEFORGE_STORAGE_OM_SERVICE"] == "seaweedfs"
    assert exports["OPENLAKEFORGE_STORAGE_DISPLAY_NAME"] == "SeaweedFS S3"
    assert exports["AWS_ENDPOINT_URL_S3"] == "http://seaweedfs-s3.olf-system:8333"
    assert exports["AWS_ALLOW_HTTP"] == "true"
    assert exports["CODE_BUCKET_NAME"] == "openlakeforge-ops"
    # Superset's Trino connection reuses the same identity dbt/Dagster use
    # (OPENLAKEFORGE_DBT_TRINO_USER) rather than a hardcoded "superset" -
    # Trino's file-based access control (modules/query/trino/main.tf) grants
    # catalog access by this exact user string, so a mismatched literal would
    # be denied once a stage-aware root provisions that access control.
    assert exports["OPENLAKEFORGE_DBT_TRINO_USER"] == "openlakeforge-dbt"
    assert (
        exports["OPENLAKEFORGE_QUERY_SQLALCHEMY_URI"] == "trino://openlakeforge-dbt@trino.olf-system:8080/iceberg"
    )
    assert unsets == []


def test_unsupported_provider_contract_version_is_rejected() -> None:
    from olf.contracts import ProviderContractError

    contracts = load_fixture("local-provider-contracts.json")
    contracts["schema_version"] = "9.0.0"
    with pytest.raises(ProviderContractError, match="unsupported"):
        build_contract_env({}, contracts, repo_root=REPO_ROOT)


def test_local_contracts_apply_seaweedfs_values() -> None:
    exports, unsets = build_contract_env({}, load_fixture("local-provider-contracts.json"), repo_root=REPO_ROOT)
    assert exports["OPENLAKEFORGE_STORAGE_PROVIDER"] == "local"
    assert exports["OPENLAKEFORGE_STORAGE_S3_SERVICE_PORT"] == "8333"
    assert exports["OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS"] == "true"
    assert exports["OPENLAKEFORGE_QUERY_TRINO_HOST"] == "trino"
    assert exports["OPENLAKEFORGE_QUERY_TRINO_PORT"] == "8080"
    assert exports["OPENLAKEFORGE_CATALOG_DATABASE_FQN"] == "polaris.lakehouse_dev"
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON"]) == {
        "sales": "polaris.lakehouse_dev.sales_silver",
        "supply_chain": "polaris.lakehouse_dev.supply_chain_silver",
    }
    assert unsets == []


def test_polaris_contract_without_namespaces_falls_back_to_the_descriptors() -> None:
    """Phase 1 no longer resolves Polaris namespaces, so every product must
    still get one from the inventory -- not just the products a stale apply
    happened to know about."""
    contracts = load_fixture("local-provider-contracts.json")
    assert "catalog_namespaces" not in contracts["catalog"]
    assert "silver_namespaces" not in contracts["catalog"]

    exports, _ = build_contract_env({}, contracts, repo_root=REPO_ROOT)

    assert json.loads(exports["OPENLAKEFORGE_CATALOG_SILVER_NAMESPACES_JSON"]) == {
        "sales": "sales_silver",
        "supply_chain": "supply_chain_silver",
    }
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_GOLD_NAMESPACES_JSON"]) == {
        "order_revenue": "order_revenue_gold",
        "customer_health": "customer_health_gold",
        "inventory_reliability": "inventory_reliability_gold",
    }
    namespaces = json.loads(exports["OPENLAKEFORGE_CATALOG_NAMESPACES_JSON"])
    assert {entry["name"] for entry in namespaces} == {
        "crm_bronze",
        "erp_bronze",
        "sales_silver",
        "supply_chain_silver",
        "order_revenue_gold",
        "customer_health_gold",
        "inventory_reliability_gold",
    }


def test_polaris_contract_publishes_the_phase_two_deployer_credentials() -> None:
    exports, _ = build_contract_env({}, load_fixture("local-provider-contracts.json"), repo_root=REPO_ROOT)

    assert exports["OPENLAKEFORGE_CATALOG_DEPLOYER_CREDENTIALS_SECRET_NAME"] == "polaris-deployer-creds"
    assert exports["OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_ID_KEY"] == "POLARIS_DEPLOYER_CLIENT_ID"
    assert exports["OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_SECRET_KEY"] == "POLARIS_DEPLOYER_CLIENT_SECRET"


def test_disabled_governance_and_analytics_unset_their_runtime_dependencies() -> None:
    contracts = load_fixture("local-provider-contracts.json")
    contracts["governance"] = {"enabled": False}
    contracts["reporting"] = {"enabled": False}

    exports, unsets = build_contract_env({}, contracts, repo_root=REPO_ROOT)

    assert exports["OPENLAKEFORGE_GOVERNANCE_ENABLED"] == "false"
    assert exports["OPENLAKEFORGE_ANALYTICS_ENABLED"] == "false"
    for name in (
        "OPENLINEAGE_URL",
        "OPENLINEAGE_ENDPOINT",
        "OPENLINEAGE_NAMESPACE",
        "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
        "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
    ):
        assert name not in exports
        assert name in unsets


def test_aws_contracts_blank_local_only_fields_and_derive_glue_fqns() -> None:
    exports, unsets = build_contract_env({}, load_fixture("aws-provider-contracts.json"), repo_root=REPO_ROOT)
    # storage.aws_s3 blanks S3-compatible endpoint plumbing
    assert exports["OPENLAKEFORGE_STORAGE_ENDPOINT"] == ""
    assert exports["OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME"] == ""
    assert exports["OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS"] == "false"
    assert "AWS_ENDPOINT_URL_S3" in unsets
    # glue catalog blanks Polaris OAuth plumbing
    assert exports["OPENLAKEFORGE_CATALOG_TOKEN_URI"] == ""
    assert exports["OPENLAKEFORGE_CATALOG_OAUTH_SCOPE"] == ""
    assert exports["OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME"] == ""
    assert exports["OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY"] == ""
    assert exports["OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY"] == ""
    assert "OPENLAKEFORGE_CATALOG_DBT_CREDENTIALS_SECRET_NAME" not in exports
    assert "OPENLAKEFORGE_CATALOG_DBT_CLIENT_ID_KEY" not in exports
    assert "OPENLAKEFORGE_CATALOG_DBT_CLIENT_SECRET_KEY" not in exports
    assert exports["OPENLAKEFORGE_CATALOG_WAREHOUSE"] == "lakehouse_dev"
    assert exports["OPENLAKEFORGE_CATALOG_GLUE_REST_URI"] == "https://glue.eu-west-1.amazonaws.com/iceberg"
    assert exports["OPENLAKEFORGE_CATALOG_GLUE_REST_WAREHOUSE"] == "123456789012"
    assert exports["OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX"] == "warehouse/iceberg"
    # OpenMetadata mappings follow the Glue provider
    assert exports["OPENLAKEFORGE_CATALOG_DATABASE_FQN"] == "aws_glue.lakehouse_dev"
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON"]) == {
        "sales": "aws_glue.lakehouse_dev.sales_silver"
    }
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON"]) == {
        "order_revenue": "aws_glue.lakehouse_dev.order_revenue_gold"
    }
    assert exports["OPENMETADATA_CATALOG_SERVICE"] == "aws_glue"
    assert exports["OPENLAKEFORGE_STORAGE_OM_SERVICE"] == "aws_s3"
    assert exports["OPENLAKEFORGE_STORAGE_DISPLAY_NAME"] == "AWS S3"
    assert exports["CODE_BUCKET_NAME"] == "openlakeforge-poc-ops"
    # AWS_REGION must follow the resolved contract storage region, not the
    # us-east-1 default from the pre-contract pass.
    assert exports["AWS_REGION"] == "eu-west-1"
    assert exports["AWS_DEFAULT_REGION"] == "eu-west-1"


def test_aws_glue_contract_without_schema_fqns_falls_back_to_the_descriptors() -> None:
    """Glue database lifecycle also moved to Phase 2 (ADR 0002): when a Glue
    contract carries no schema-FQN map at all -- not the partial single-product
    map the fixture normally carries -- the same descriptor-driven fallback
    that serves Polaris must serve Glue, using the aws_glue.<catalog> prefix.

    Every physical database name is itself prefixed with the stage's own
    catalog_name ("lakehouse_dev_sales_silver", not bare "sales_silver"):
    this account's Glue service refuses to create a catalog per stage, so
    every stage shares the account's one default catalog, and the database
    name is what stays collision-free across stages instead."""
    contracts = load_fixture("aws-provider-contracts.json")
    del contracts["catalog"]["silver_schema_fqns"]
    del contracts["catalog"]["gold_schema_fqns"]

    exports, _ = build_contract_env({}, contracts, repo_root=REPO_ROOT)

    assert json.loads(exports["OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON"]) == {
        "sales": "aws_glue.lakehouse_dev.lakehouse_dev_sales_silver",
        "supply_chain": "aws_glue.lakehouse_dev.lakehouse_dev_supply_chain_silver",
    }
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON"]) == {
        "order_revenue": "aws_glue.lakehouse_dev.lakehouse_dev_order_revenue_gold",
        "customer_health": "aws_glue.lakehouse_dev.lakehouse_dev_customer_health_gold",
        "inventory_reliability": "aws_glue.lakehouse_dev.lakehouse_dev_inventory_reliability_gold",
    }


def test_caller_exported_aws_region_is_preserved() -> None:
    exports, _ = build_contract_env(
        {"AWS_REGION": "us-west-2"}, load_fixture("aws-provider-contracts.json"), repo_root=REPO_ROOT
    )
    assert exports["AWS_REGION"] == "us-west-2"


def test_caller_environment_wins_for_user_settable_variables() -> None:
    base = {
        "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI": "trino://custom@example:8080/iceberg",
        "OPENMETADATA_CATALOG_SERVICE": "custom_service",
        "OPENLAKEFORGE_STORAGE_OM_SERVICE": "custom_storage",
        "OPENLAKEFORGE_STORAGE_DISPLAY_NAME": "Custom S3",
    }
    exports, _ = build_contract_env(base, load_fixture("aws-provider-contracts.json"), repo_root=REPO_ROOT)
    assert "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI" not in exports
    assert "OPENMETADATA_CATALOG_SERVICE" not in exports
    assert "OPENLAKEFORGE_STORAGE_OM_SERVICE" not in exports
    assert "OPENLAKEFORGE_STORAGE_DISPLAY_NAME" not in exports


def test_contract_values_override_inherited_environment() -> None:
    base = {"OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "stale-bucket"}
    exports, _ = build_contract_env(base, load_fixture("aws-provider-contracts.json"), repo_root=REPO_ROOT)
    assert exports["OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"] == "openlakeforge-poc-bronze"


def test_namespace_env_feeds_kube_namespace_default() -> None:
    exports, _ = build_contract_env({"NAMESPACE": "custom-ns"}, None, repo_root=REPO_ROOT)
    assert exports["OPENLAKEFORGE_KUBE_NAMESPACE"] == "custom-ns"


@pytest.mark.parametrize("fixture", ["local-provider-contracts.json", "aws-provider-contracts.json"])
def test_render_shell_exports_are_evaluable_lines(fixture: str) -> None:
    exports, unsets = build_contract_env({}, load_fixture(fixture), repo_root=REPO_ROOT)
    output = render_shell_exports(exports, unsets)
    for line in output.splitlines():
        assert line.startswith("export ") or line.startswith("unset ")


def test_a_v2_contract_refuses_to_answer_for_another_stage() -> None:
    """v2 carries one stage's bindings with no stage index. Serving it for
    another stage would hand that stage DEV's namespace, catalog, and
    capability flags while reporting success."""
    from olf.contracts import ProviderContractError
    from olf.deployment.context import Provider
    from olf.profile import Preset, StageName, legacy_single_stage_topology

    contracts = load_fixture("local-provider-contracts.json")
    topology = legacy_single_stage_topology(provider=Provider.LOCAL, preset=Preset.SLIM)

    with pytest.raises(ProviderContractError, match="describes only the DEV stage"):
        build_contract_env({}, contracts, repo_root=REPO_ROOT, topology=topology, stage=StageName.PROD)

    # The DEV stage still resolves, as does the unselected default.
    build_contract_env({}, contracts, repo_root=REPO_ROOT, topology=topology, stage=StageName.DEV)
    build_contract_env({}, contracts, repo_root=REPO_ROOT)


def test_provenance_is_recorded_only_for_an_applied_contract() -> None:
    """A stage marker left in the caller's shell must not survive a run whose
    Terraform output was unavailable, or a consumer would read it as proof
    that the synthesized defaults came from a real contract."""
    exports, unsets = build_contract_env(
        {"OPENLAKEFORGE_CONTRACT_STAGE": "prod"}, None, repo_root=REPO_ROOT
    )

    assert "OPENLAKEFORGE_CONTRACT_STAGE" not in exports
    assert "OPENLAKEFORGE_CONTRACT_STAGE" in unsets


def test_a_contract_file_replaces_terraform_state_and_fails_closed_when_missing(tmp_path: Path) -> None:
    """A CI deploy job has no platform Terraform state (#119); a file it named
    but cannot read must never read as "not applied yet"."""
    from olf.contracts import PROVIDER_CONTRACTS_FILE_ENV, load_provider_contracts
    from olf.provider_contracts import ProviderContractError

    fixture = FIXTURES / "conformance-provider-contracts.json"
    loaded = load_provider_contracts("/nonexistent", environ={PROVIDER_CONTRACTS_FILE_ENV: str(fixture)})
    assert loaded == json.loads(fixture.read_text())

    # A provider's scoped env drops ambient variables; the CI job's selector still applies.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(PROVIDER_CONTRACTS_FILE_ENV, str(fixture))
        assert load_provider_contracts("/nonexistent", environ={"KUBE_CONTEXT": "kind"}) == loaded

    with pytest.raises(ProviderContractError, match=PROVIDER_CONTRACTS_FILE_ENV):
        load_provider_contracts("/nonexistent", environ={PROVIDER_CONTRACTS_FILE_ENV: str(tmp_path / "missing.json")})
