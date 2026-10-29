import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from libs import floe_job  # noqa: E402
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
    # stands: `lakehouse_dev_sales_silver`, as `olf catalog sync-namespaces` names it.
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
    assert runner["env"]["OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX"] == "lakehouse_dev_"


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
    assert runner["command"] == "olf-floe-job"


def test_the_job_gets_the_contract_values_and_credentials_by_reference_only() -> None:
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
    runner = _runner(environ)

    assert runner["env"]["OPENLAKEFORGE_CATALOG_REST_URI"] == _STAGE["OPENLAKEFORGE_CATALOG_REST_URI"]
    assert runner["env"]["AWS_REGION"] == "us-east-1"
    assert "must-not-appear" not in str(runner)
    assert {(s["name"], s["secret_name"], s["key"]) for s in runner["secrets"]} == {
        ("POLARIS_FLOE_CLIENT_ID", "polaris-dev-floe-creds", "POLARIS_FLOE_CLIENT_ID"),
        ("POLARIS_FLOE_CLIENT_SECRET", "polaris-dev-floe-creds", "POLARIS_FLOE_CLIENT_SECRET"),
        ("AWS_ACCESS_KEY_ID", "seaweedfs-dev-s3-creds", "AWS_ACCESS_KEY_ID"),
        ("AWS_SECRET_ACCESS_KEY", "seaweedfs-dev-s3-creds", "AWS_SECRET_ACCESS_KEY"),
        ("OPENLINEAGE_API_KEY", "openmetadata-ingestion-bot", "OPENMETADATA_INGESTION_BOT_JWT"),
    }


def test_the_job_renders_the_same_stage_profile_from_what_it_was_given() -> None:
    environ = {**_STAGE, "AWS_REGION": "us-east-1", "OPENLINEAGE_URL": "http://openmetadata:8585"}
    runner = _runner(environ)
    in_job = render_profile({**runner["env"], "POLARIS_FLOE_CLIENT_ID": "x"}, in_job=True)
    outside = render_profile(environ)

    assert in_job["execution"]["runner"] == {"type": "local"}
    for section in ("storages", "catalogs", "lineage"):
        assert in_job[section] == outside[section]


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


def test_the_job_entrypoint_renders_the_profile_where_floe_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {**_STAGE, "AWS_REGION": "us-east-1", "POLARIS_FLOE_CLIENT_ID": "x"}.items():
        monkeypatch.setenv(name, value)
    executed: list[list[str]] = []
    monkeypatch.setattr(floe_job.os, "execvp", lambda _file, argv: executed.append(argv))
    profile = tmp_path / "openlakeforge-floe" / "profile.yml"
    args = ["run", "-c", "/opt/project/floe.yml", "-p", str(profile), "--quiet", "--entities", "orders,accounts"]

    floe_job.main(args)

    assert executed == [["floe", *args]]
    assert json.loads(profile.read_text())["execution"]["runner"] == {"type": "local"}
