import json
from pathlib import Path

import pytest

from olf.common.profile import resolve_topology, validate_deployment_profile
from olf.infra.contracts import build_contract_env as _build_contract_env
from olf.infra.contracts import render_shell_exports

FIXTURES = Path(__file__).parents[1] / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[4]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def build_contract_env(base: dict, contracts: dict | None, *, repo_root: Path) -> tuple[dict, list]:
    """Resolve the DEV stage of a native v3 contract against the topology it describes."""
    if contracts is None:
        return _build_contract_env(base, None, repo_root=repo_root)
    deployment = contracts["deployment"]
    topology = resolve_topology(
        validate_deployment_profile(
            {
                "apiVersion": "openlakeforge.io/v1alpha1",
                "kind": "DeploymentProfile",
                "metadata": {"name": deployment["profile_name"]},
                "spec": {
                    "provider": {
                        "type": deployment["provider"],
                        **({"region": deployment["region"]} if deployment["region"] else {}),
                    },
                    "preset": "slim",
                    **(
                        {"access": {"tls": "local-ca"}}
                        if contracts["shared"]["access"]["implementation"] == "access.ingress"
                        else {}
                    ),
                    # Authentication off is local-only, so cloud fixtures carry an identity block.
                    **({"identity": _EXTERNAL_IDENTITY} if deployment["provider"] != "local" else {}),
                    "stages": {
                        name: {
                            "enabled": True,
                            "capabilities": {"analytics": "reporting" in stage, "governance": "governance" in stage},
                        }
                        for name, stage in contracts["stages"].items()
                    },
                },
            }
        )
    )
    return _build_contract_env(base, contracts, repo_root=repo_root, topology=topology, stage="dev")


_EXTERNAL_IDENTITY = {
    "issuer": "external",
    "issuer_url": "https://login.example.com/",
    "role_claim": "groups",
    "role_mapping": {"platform-admin": ["olf-admins"]},
}


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
    from olf.common.provider_contracts import ProviderContractError

    contracts = load_fixture("local-provider-contracts-v3.json")
    contracts["schema_version"] = "9.0.0"
    with pytest.raises(ProviderContractError, match="unsupported"):
        build_contract_env({}, contracts, repo_root=REPO_ROOT)


def test_local_contracts_apply_seaweedfs_values() -> None:
    exports, unsets = build_contract_env({}, load_fixture("local-provider-contracts-v3.json"), repo_root=REPO_ROOT)
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


def test_polaris_contract_publishes_the_phase_two_deployer_credentials() -> None:
    exports, _ = build_contract_env({}, load_fixture("local-provider-contracts-v3.json"), repo_root=REPO_ROOT)

    assert exports["OPENLAKEFORGE_CATALOG_DEPLOYER_CREDENTIALS_SECRET_NAME"] == "polaris-deployer-creds"
    assert exports["OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_ID_KEY"] == "POLARIS_DEPLOYER_CLIENT_ID"
    assert exports["OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_SECRET_KEY"] == "POLARIS_DEPLOYER_CLIENT_SECRET"


def test_disabled_governance_and_analytics_unset_their_runtime_dependencies() -> None:
    contracts = load_fixture("local-provider-contracts-v3.json")
    assert "governance" not in contracts["stages"]["dev"]
    assert "reporting" not in contracts["stages"]["dev"]

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


def test_dbt_runs_through_the_openlineage_wrapper_only_where_governance_is_on() -> None:
    off, _ = build_contract_env({}, load_fixture("local-provider-contracts-v3.json"), repo_root=REPO_ROOT)
    on, _ = build_contract_env({}, load_fixture("aws-provider-contracts-v3.json"), repo_root=REPO_ROOT)

    assert off["OPENLAKEFORGE_GOVERNANCE_ENABLED"] == "false"
    assert off["OPENLAKEFORGE_DBT_EXECUTABLE"] == "dbt"
    assert on["OPENLAKEFORGE_GOVERNANCE_ENABLED"] == "true"
    assert on["OPENLAKEFORGE_DBT_EXECUTABLE"] == "dbt-ol"


def test_an_explicit_dbt_executable_wins_over_the_governance_default() -> None:
    exports, _ = build_contract_env(
        {"OPENLAKEFORGE_DBT_EXECUTABLE": "/opt/dbt"},
        load_fixture("local-provider-contracts-v3.json"),
        repo_root=REPO_ROOT,
    )

    assert exports["OPENLAKEFORGE_DBT_EXECUTABLE"] == "/opt/dbt"


def test_aws_contracts_blank_local_only_fields_and_derive_glue_fqns() -> None:
    exports, unsets = build_contract_env({}, load_fixture("aws-provider-contracts-v3.json"), repo_root=REPO_ROOT)
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
    assert exports["OPENLAKEFORGE_CATALOG_GLUE_REST_URI"] == "https://glue.eu-west-3.amazonaws.com/iceberg"
    assert exports["OPENLAKEFORGE_CATALOG_GLUE_REST_WAREHOUSE"] == "123456789012"
    assert exports["OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX"] == "warehouse/iceberg"
    # OpenMetadata mappings follow the Glue provider
    assert exports["OPENLAKEFORGE_CATALOG_DATABASE_FQN"] == "aws_glue.lakehouse_dev"
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON"]) == {
        "sales": "aws_glue.lakehouse_dev.lakehouse_dev_sales_silver",
        "supply_chain": "aws_glue.lakehouse_dev.lakehouse_dev_supply_chain_silver",
    }
    assert json.loads(exports["OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON"]) == {
        "order_revenue": "aws_glue.lakehouse_dev.lakehouse_dev_order_revenue_gold",
        "customer_health": "aws_glue.lakehouse_dev.lakehouse_dev_customer_health_gold",
        "inventory_reliability": "aws_glue.lakehouse_dev.lakehouse_dev_inventory_reliability_gold",
    }
    assert exports["OPENMETADATA_CATALOG_SERVICE"] == "aws_glue"
    assert exports["OPENLAKEFORGE_STORAGE_OM_SERVICE"] == "aws_s3"
    assert exports["OPENLAKEFORGE_STORAGE_DISPLAY_NAME"] == "AWS S3"
    assert exports["CODE_BUCKET_NAME"] == "acme-ops"
    # AWS_REGION must follow the resolved contract storage region, not the
    # us-east-1 default from the pre-contract pass.
    assert exports["AWS_REGION"] == "eu-west-3"
    assert exports["AWS_DEFAULT_REGION"] == "eu-west-3"


def test_caller_exported_aws_region_is_preserved() -> None:
    exports, _ = build_contract_env(
        {"AWS_REGION": "us-west-2"}, load_fixture("aws-provider-contracts-v3.json"), repo_root=REPO_ROOT
    )
    assert exports["AWS_REGION"] == "us-west-2"


def test_caller_environment_wins_for_user_settable_variables() -> None:
    base = {
        "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI": "trino://custom@example:8080/iceberg",
        "OPENMETADATA_CATALOG_SERVICE": "custom_service",
        "OPENLAKEFORGE_STORAGE_OM_SERVICE": "custom_storage",
        "OPENLAKEFORGE_STORAGE_DISPLAY_NAME": "Custom S3",
    }
    exports, _ = build_contract_env(base, load_fixture("aws-provider-contracts-v3.json"), repo_root=REPO_ROOT)
    assert "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI" not in exports
    assert "OPENMETADATA_CATALOG_SERVICE" not in exports
    assert "OPENLAKEFORGE_STORAGE_OM_SERVICE" not in exports
    assert "OPENLAKEFORGE_STORAGE_DISPLAY_NAME" not in exports


def test_contract_values_override_inherited_environment() -> None:
    base = {"OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "stale-bucket"}
    exports, _ = build_contract_env(base, load_fixture("aws-provider-contracts-v3.json"), repo_root=REPO_ROOT)
    assert exports["OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"] == "acme-data-dev-bronze"


def test_namespace_env_feeds_kube_namespace_default() -> None:
    exports, _ = build_contract_env({"NAMESPACE": "custom-ns"}, None, repo_root=REPO_ROOT)
    assert exports["OPENLAKEFORGE_KUBE_NAMESPACE"] == "custom-ns"


@pytest.mark.parametrize("fixture", ["local-provider-contracts-v3.json", "aws-provider-contracts-v3.json"])
def test_render_shell_exports_are_evaluable_lines(fixture: str) -> None:
    exports, unsets = build_contract_env({}, load_fixture(fixture), repo_root=REPO_ROOT)
    output = render_shell_exports(exports, unsets)
    for line in output.splitlines():
        assert line.startswith("export ") or line.startswith("unset ")


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
    from olf.common.contract_state import PROVIDER_CONTRACTS_FILE_ENV, load_provider_contracts
    from olf.common.provider_contracts import ProviderContractError

    fixture = FIXTURES / "conformance-provider-contracts.json"
    loaded = load_provider_contracts("/nonexistent", environ={PROVIDER_CONTRACTS_FILE_ENV: str(fixture)})
    assert loaded == json.loads(fixture.read_text())

    # A provider's scoped env drops ambient variables; the CI job's selector still applies.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(PROVIDER_CONTRACTS_FILE_ENV, str(fixture))
        assert load_provider_contracts("/nonexistent", environ={"KUBE_CONTEXT": "kind"}) == loaded

    with pytest.raises(ProviderContractError, match=PROVIDER_CONTRACTS_FILE_ENV):
        load_provider_contracts("/nonexistent", environ={PROVIDER_CONTRACTS_FILE_ENV: str(tmp_path / "missing.json")})


def test_a_contract_without_a_credential_secret_unsets_a_stale_one() -> None:
    stale = {"OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET": "trino-credential"}

    exports, unsets = build_contract_env(stale, load_fixture("local-provider-contracts-v3.json"), repo_root=REPO_ROOT)

    assert "OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET" not in exports
    assert "OPENLAKEFORGE_QUERY_TRINO_CREDENTIAL_SECRET" in unsets


def test_each_stage_reads_its_own_query_endpoint() -> None:
    contract = load_fixture("local-provider-contracts-v3.json")
    for name, stage in contract["stages"].items():
        stage["query"]["service_ref"] = f"stage/{name}/query"
        stage["query"]["endpoint"] = f"http://trino-{name}.olf-{name}:8080"
        stage["endpoints"]["query"] = f"stage/{name}/query"

    exports, _ = build_contract_env({}, contract, repo_root=REPO_ROOT)

    assert exports["OPENLAKEFORGE_QUERY_TRINO_HOST"] == "trino-dev.olf-dev"
    assert "trino-dev.olf-dev" in exports["OPENLAKEFORGE_QUERY_SQLALCHEMY_URI"]
