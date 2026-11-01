import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from libs.floe_profile import FloeProfileError, render_profile  # noqa: E402

_STAGE = {
    "OPENLAKEFORGE_STAGE": "dev",
    "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "lakehouse-bronze",
    "OPENLAKEFORGE_STORAGE_SILVER_BUCKET": "lakehouse-silver",
    "OPENLAKEFORGE_ARTIFACT_BASE_URI": "s3://openlakeforge-ops/activations/dev",
    "OPENLAKEFORGE_CATALOG_REST_URI": "http://polaris:8181/api/catalog",
    "OPENLAKEFORGE_CATALOG_TOKEN_URI": "http://polaris:8181/api/catalog/v1/oauth/tokens",
    "OPENLAKEFORGE_CATALOG_NAME": "lakehouse_dev",
    "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME": "polaris-dev-floe-creds",
    "OPENLAKEFORGE_PROJECT_CODE_IMAGE": "ghcr.io/acme/project-code@sha256:" + "a" * 64,
    "OPENLAKEFORGE_KUBE_NAMESPACE": "olf-dev",
}


def _storages(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {storage["name"]: storage for storage in profile["storages"]["definitions"]}


def test_activation_contract_env_binds_storages_and_lineage() -> None:
    profile = render_profile(
        {
            **_STAGE,
            "OPENLAKEFORGE_STORAGE_REGION": "us-east-1",
            "OPENLAKEFORGE_STORAGE_ENDPOINT": "http://seaweedfs:8333",
            "OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS": "true",
            "OPENLAKEFORGE_GOVERNANCE_ENABLED": "true",
            "OPENLINEAGE_URL": "http://openmetadata:8585",
        }
    )

    ops = _storages(profile)["openlakeforge_ops"]
    assert ops["bucket"] == "openlakeforge-ops"
    assert ops["prefix"] == "activations/dev"
    assert ops["endpoint"] == "http://seaweedfs:8333"
    assert ops["path_style_access"] is True
    assert profile["lineage"]["url"] == "http://openmetadata:8585"


def test_terraform_managed_env_falls_back_to_the_aws_aliases() -> None:
    # The Terraform-managed deployment path sets the AWS SDK names only and
    # exports OPENLINEAGE_URL instead of the governance flag.
    profile = render_profile(
        {
            **_STAGE,
            "AWS_REGION": "eu-west-1",
            "AWS_ENDPOINT_URL_S3": "http://seaweedfs:8333",
            "AWS_S3_FORCE_PATH_STYLE": "true",
            "OPENLINEAGE_URL": "http://openmetadata:8585",
        }
    )

    silver = _storages(profile)["lakehouse_silver"]
    assert silver["region"] == "eu-west-1"
    assert silver["endpoint"] == "http://seaweedfs:8333"
    assert silver["path_style_access"] is True
    assert profile["lineage"]["url"] == "http://openmetadata:8585"


def test_slim_terraform_env_without_lineage_url_renders_no_lineage() -> None:
    profile = render_profile({**_STAGE, "AWS_DEFAULT_REGION": "us-east-1"})

    assert "lineage" not in profile


def test_governance_flag_without_lineage_url_is_refused() -> None:
    with pytest.raises(FloeProfileError, match="OPENLINEAGE_URL"):
        render_profile({**_STAGE, "AWS_REGION": "us-east-1", "OPENLAKEFORGE_GOVERNANCE_ENABLED": "true"})


def test_missing_region_is_refused() -> None:
    with pytest.raises(FloeProfileError, match="OPENLAKEFORGE_STORAGE_REGION"):
        render_profile(_STAGE)


_GLUE_STAGE = {
    **_STAGE,
    "AWS_REGION": "eu-west-1",
    "OPENLAKEFORGE_CATALOG_TYPE": "glue",
    "OPENLAKEFORGE_CATALOG_PROVIDER": "aws-glue",
    "OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX": "lakehouse_dev_",
    "OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX": "warehouse/iceberg",
    "OPENLINEAGE_URL": "http://openmetadata:8585",
}


def test_a_glue_stage_writes_each_domain_into_its_own_prefixed_database() -> None:
    profile = render_profile(_GLUE_STAGE)

    (catalog,) = profile["catalogs"]["definitions"]
    # Floe puts the domain's Silver namespace (`sales_silver`) where `{domain}`
    # stands: `lakehouse_dev_sales_silver`, as `olf infra catalog sync-namespaces` names it.
    assert catalog == {
        "name": "iceberg_catalog",
        "type": "glue",
        "region": "eu-west-1",
        "database": "lakehouse_dev_{domain}",
        "create_database_if_missing": False,
        "warehouse_storage": "lakehouse_silver",
        "warehouse_prefix": "s3://lakehouse-silver/warehouse/iceberg",
    }
    assert profile["lineage"]["dataset_namespace"] == "aws_glue"


def test_a_glue_stage_without_its_database_prefix_is_refused() -> None:
    # Unprefixed, every stage would share one database per domain in the account's one catalog.
    environ = {name: value for name, value in _GLUE_STAGE.items() if name != "OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX"}

    with pytest.raises(FloeProfileError, match="OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX"):
        render_profile(environ)


def test_a_glue_job_carries_no_polaris_credentials() -> None:
    runner = render_profile(_GLUE_STAGE)["execution"]["runner"]

    assert not [secret for secret in runner["secrets"] if secret["name"].startswith("POLARIS_")]


def test_a_catalog_floe_cannot_write_through_is_refused() -> None:
    with pytest.raises(FloeProfileError, match="REST or AWS Glue catalog"):
        render_profile({**_STAGE, "AWS_REGION": "us-east-1", "OPENLAKEFORGE_CATALOG_TYPE": "unity"})


def test_an_empty_oauth_scope_falls_back_to_all_principal_roles() -> None:
    # Terraform renders an unset contract scope as an empty string.
    profile = render_profile({**_STAGE, "AWS_REGION": "us-east-1", "OPENLAKEFORGE_CATALOG_OAUTH_SCOPE": ""})

    assert profile["catalogs"]["definitions"][0]["scope"] == "PRINCIPAL_ROLE:ALL"


def _runner(environ: dict[str, str]) -> dict[str, Any]:
    return render_profile(environ)["execution"]["runner"]


def test_floe_runs_one_kubernetes_job_per_domain_on_the_project_code_image() -> None:
    runner = _runner({**_STAGE, "AWS_REGION": "us-east-1"})

    assert runner["type"] == "kubernetes_job"
    assert runner["job_scope"] == "domain"
    assert runner["image"] == _STAGE["OPENLAKEFORGE_PROJECT_CODE_IMAGE"]
    assert runner["namespace"] == "olf-dev"
    assert runner["service_account"] == "dagster"
    # dagster-floe hands the Job the profile's text, so it runs Floe's own `floe` with no wrapper.
    assert "command" not in runner


def test_a_domain_job_runs_up_to_four_entities_at_once() -> None:
    profile = render_profile({**_STAGE, "AWS_REGION": "us-east-1"})

    # Floe 0.8.8 runs merge entities one at a time whatever the limit, so the
    # Job's 4 GB DuckDB cap still covers one merge at most.
    assert profile["execution"]["orchestration"] == {"strategy": "parallel", "max_concurrent_entities": 4}


def test_the_job_names_its_region_and_skips_the_metadata_service_on_self_hosted_storage() -> None:
    environ = {**_STAGE, "AWS_REGION": "us-east-1"}

    self_hosted = _runner({**environ, "OPENLAKEFORGE_STORAGE_ENDPOINT": "http://seaweedfs-s3.olf-system:8333"})
    on_aws = _runner(
        {
            key: value
            for key, value in environ.items()
            if key not in ("OPENLAKEFORGE_STORAGE_ENDPOINT", "AWS_ENDPOINT_URL_S3")
        }
    )

    assert self_hosted["env"] == {
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    assert on_aws["env"] == {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"}


def test_the_job_is_polled_often_enough_to_end_its_step_promptly() -> None:
    runner = _runner({**_STAGE, "AWS_REGION": "us-east-1"})

    # Each poll interval is wall-clock time added to the end of every Silver step.
    assert runner["poll_interval_seconds"] == 2


def test_the_job_memory_limit_leaves_room_above_floes_duckdb_cap() -> None:
    resources = _runner({**_STAGE, "AWS_REGION": "us-east-1"})["resources"]

    # Floe 0.8.4 caps DuckDB at 4 GB and asks for pods sized at that + 2 GiB.
    assert resources["memory_limit_mb"] >= 6 * 1024
    assert resources["memory_mb"] <= resources["memory_limit_mb"]
    assert "cpu_limit" not in resources


def test_the_job_gets_credentials_by_reference_only() -> None:
    environ = {
        **_STAGE,
        "AWS_REGION": "us-east-1",
        "OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME": "seaweedfs-dev-s3-creds",
        "OPENLAKEFORGE_GOVERNANCE_ENABLED": "true",
        "OPENLINEAGE_URL": "http://openmetadata:8585",
        "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME": "openmetadata-ingestion-bot",
        "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY": "OPENMETADATA_INGESTION_BOT_JWT",
        "AWS_SECRET_ACCESS_KEY": "must-not-appear",
        "POLARIS_FLOE_CLIENT_SECRET": "must-not-appear",
    }
    profile = render_profile(environ)
    runner = profile["execution"]["runner"]

    # The Job reads this whole profile, so no credential value may enter it.
    assert "must-not-appear" not in str(profile)
    assert {(s["name"], s["secret_name"], s["key"]) for s in runner["secrets"]} == {
        ("POLARIS_FLOE_CLIENT_ID", "polaris-dev-floe-creds", "POLARIS_FLOE_CLIENT_ID"),
        ("POLARIS_FLOE_CLIENT_SECRET", "polaris-dev-floe-creds", "POLARIS_FLOE_CLIENT_SECRET"),
        ("AWS_ACCESS_KEY_ID", "seaweedfs-dev-s3-creds", "AWS_ACCESS_KEY_ID"),
        ("AWS_SECRET_ACCESS_KEY", "seaweedfs-dev-s3-creds", "AWS_SECRET_ACCESS_KEY"),
        ("OPENLINEAGE_API_KEY", "openmetadata-ingestion-bot", "OPENMETADATA_INGESTION_BOT_JWT"),
    }


def test_terraform_managed_pods_name_the_namespace_and_image_their_own_way() -> None:
    environ = {**_STAGE, "AWS_REGION": "us-east-1", "NAMESPACE": "olf-prod", "DAGSTER_CURRENT_IMAGE": "img:local"}
    del environ["OPENLAKEFORGE_KUBE_NAMESPACE"], environ["OPENLAKEFORGE_PROJECT_CODE_IMAGE"]

    runner = _runner(environ)

    assert (runner["namespace"], runner["image"]) == ("olf-prod", "img:local")


def test_a_pod_that_cannot_name_its_image_is_refused() -> None:
    environ = {**_STAGE, "AWS_REGION": "us-east-1"}
    del environ["OPENLAKEFORGE_PROJECT_CODE_IMAGE"]

    with pytest.raises(FloeProfileError, match="OPENLAKEFORGE_PROJECT_CODE_IMAGE"):
        render_profile(environ)
