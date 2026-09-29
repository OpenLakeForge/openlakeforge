from pathlib import Path

import pytest
import yaml

from olf.floe import render_profile

REPO_ROOT = Path(__file__).resolve().parents[3]

AWS_ENV = {
    "OPENLAKEFORGE_STORAGE_IMPLEMENTATION": "storage.aws_s3",
    "OPENLAKEFORGE_STORAGE_PROVIDER": "aws",
    "OPENLAKEFORGE_STORAGE_REGION": "eu-west-1",
    "OPENLAKEFORGE_STORAGE_ENDPOINT": "",
    "OPENLAKEFORGE_STORAGE_VIRTUAL_HOST_ENDPOINT": "",
    "OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS": "false",
    "OPENLAKEFORGE_STORAGE_SSL_MODE": "required",
    "OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME": "",
    "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "openlakeforge-poc-bronze",
    "OPENLAKEFORGE_STORAGE_SILVER_BUCKET": "openlakeforge-poc-silver",
    "OPENLAKEFORGE_OPS_BUCKET_NAME": "openlakeforge-poc-ops",
    "OPENLAKEFORGE_CATALOG_TYPE": "glue",
    "OPENLAKEFORGE_CATALOG_PROVIDER": "aws-glue",
    "OPENLAKEFORGE_CATALOG_GLUE_REGION": "eu-west-1",
    "OPENLAKEFORGE_CATALOG_GLUE_DATABASE": "sales_customer_health_silver",
}


def test_local_profile_uses_polaris_rest_catalog_and_secrets() -> None:
    profile = render_profile({})
    assert 'name: "local-k8s"' in profile
    assert 'type: "rest"' in profile
    assert 'default: "iceberg_catalog"' in profile
    assert "image: ghcr.io/malon64/floe:0.6.11" in profile
    assert 'warehouse_prefix: "s3://lakehouse-silver"' in profile
    assert "secret_name: seaweedfs-s3-creds" in profile
    assert "secret_name: polaris-floe-creds" in profile
    assert "secret_name: openmetadata-ingestion-bot" in profile
    assert "name: OPENLINEAGE_API_KEY" in profile
    assert 'url: "http://openmetadata.olf-system:8585"' in profile
    assert 'endpoint: "api/v1/openlineage/lineage"' in profile
    assert 'dataset_namespace: "polaris"' in profile
    assert "api_key:" not in profile
    assert 'AWS_ALLOW_HTTP: "true"' in profile
    assert 'AWS_ENDPOINT_URL: "http://seaweedfs-s3.olf-system:8333"' in profile
    assert 'AWS_ENDPOINT_URL_S3: "http://seaweedfs-s3.olf-system:8333"' in profile
    assert "\nstorages:" not in profile


def test_checked_in_dev_profile_uses_the_preserved_storage_identities() -> None:
    profile = yaml.safe_load((REPO_ROOT / "libs/floe/profiles/local-k8s.yml").read_text())

    assert profile["variables"]["OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"] == "lakehouse-bronze"
    assert profile["variables"]["OPENLAKEFORGE_STORAGE_SILVER_BUCKET"] == "lakehouse-silver"
    assert profile["catalogs"]["definitions"][0]["warehouse_prefix"] == "s3://lakehouse-silver"


def test_aws_profile_uses_glue_catalog_without_secrets() -> None:
    profile = render_profile(AWS_ENV)
    assert 'name: "aws-eks"' in profile
    assert 'type: "glue"' in profile
    assert "image: ghcr.io/malon64/floe:0.6.11" in profile
    assert 'database: "sales_customer_health_silver"' in profile
    assert 'warehouse_prefix: "s3://openlakeforge-poc-silver/warehouse/iceberg"' in profile
    assert "secret_name: openmetadata-ingestion-bot" in profile
    assert "name: OPENLINEAGE_API_KEY" in profile
    assert 'dataset_namespace: "aws_glue"' in profile
    assert "api_key:" not in profile
    assert "AWS_ENDPOINT_URL" not in profile
    assert 'AWS_EC2_METADATA_DISABLED: "false"' in profile


def test_aws_glue_profile_requires_glue_database() -> None:
    env = dict(AWS_ENV)
    del env["OPENLAKEFORGE_CATALOG_GLUE_DATABASE"]
    with pytest.raises(SystemExit):
        render_profile(env)


@pytest.mark.parametrize(
    "environ",
    [
        {"OPENLAKEFORGE_GOVERNANCE_ENABLED": "false"},
        # A governed stage that is not the lineage stage (#131).
        {"OPENLAKEFORGE_GOVERNANCE_ENABLED": "true", "OPENLINEAGE_DISABLED": "true"},
    ],
)
def test_profile_omits_openlineage_when_governance_or_lineage_is_disabled(environ: dict[str, str]) -> None:
    profile = render_profile(environ)

    assert "lineage:" not in profile
    assert "OPENLINEAGE_API_KEY" not in profile
    assert "openmetadata-ingestion-bot" not in profile
