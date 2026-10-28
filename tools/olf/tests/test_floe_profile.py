import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
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


def test_glue_catalog_is_refused() -> None:
    with pytest.raises(FloeProfileError, match="REST catalog only"):
        render_profile({**_STAGE, "AWS_REGION": "us-east-1", "OPENLAKEFORGE_CATALOG_TYPE": "glue"})
