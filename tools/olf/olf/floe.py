"""Floe environment-profile rendering from provider contract environment values.

Port of scripts/local/contracts/render-floe-profile.py. The rendered YAML is
consumed by the Floe CLI/runner, and scripts/test/check-contracts.sh asserts on
exact rendered settings, so the output must stay byte-identical.
"""

from __future__ import annotations

import json
from collections.abc import Mapping


def _yaml_string(value: str) -> str:
    return json.dumps(str(value))


def _absolute_s3_prefix(bucket: str, prefix: str) -> str:
    if "://" in prefix:
        return prefix
    if not prefix:
        return f"s3://{bucket}"
    return f"s3://{bucket}/{prefix.lstrip('/')}"


def render_profile(environ: Mapping[str, str]) -> str:
    """Render the Floe EnvironmentProfile YAML for the active contract env."""

    def env(name: str, default: str) -> str:
        return environ.get(name, default)

    namespace = env("NAMESPACE", env("OPENLAKEFORGE_KUBE_NAMESPACE", "olf-dev"))
    catalog_name = env("OPENLAKEFORGE_CATALOG_LOGICAL_NAME", "iceberg_catalog")
    storage_bronze_bucket = env(
        "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET", env("OPENLAKEFORGE_STORAGE_BUCKET", "lakehouse-bronze")
    )
    storage_silver_bucket = env("OPENLAKEFORGE_STORAGE_SILVER_BUCKET", "lakehouse-silver")
    ops_bucket = env("OPENLAKEFORGE_OPS_BUCKET_NAME", env("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME", "openlakeforge-ops"))
    storage_region = env("OPENLAKEFORGE_STORAGE_REGION", "us-east-1")
    storage_implementation = env("OPENLAKEFORGE_STORAGE_IMPLEMENTATION", "storage.s3_compatible.seaweedfs")
    is_aws_s3 = storage_implementation == "storage.aws_s3"
    storage_endpoint = env("OPENLAKEFORGE_STORAGE_ENDPOINT", "" if is_aws_s3 else "http://seaweedfs-s3.olf-system:8333")
    storage_virtual_endpoint = env(
        "OPENLAKEFORGE_STORAGE_VIRTUAL_HOST_ENDPOINT",
        "" if is_aws_s3 else f"http://{namespace}.svc.cluster.local:8333",
    )
    catalog_type = env("OPENLAKEFORGE_CATALOG_TYPE", "rest")
    catalog_provider = env("OPENLAKEFORGE_CATALOG_PROVIDER", "polaris")
    is_aws_glue = catalog_type == "glue" and catalog_provider == "aws-glue"
    profile_name = env("OPENLAKEFORGE_FLOE_PROFILE_NAME", "aws-eks" if is_aws_glue and is_aws_s3 else "local-k8s")
    profile_env = env("OPENLAKEFORGE_FLOE_PROFILE_ENV", "aws" if is_aws_glue and is_aws_s3 else "local")
    profile_description = env(
        "OPENLAKEFORGE_FLOE_PROFILE_DESCRIPTION",
        "AWS EKS runner profile for OpenLakeForge Floe assets."
        if is_aws_glue and is_aws_s3
        else "Contract-rendered local Kubernetes runner profile for OpenLakeForge Floe assets.",
    )
    catalog_rest_uri = env("OPENLAKEFORGE_CATALOG_REST_URI", "http://polaris.olf-system:8181/api/catalog")
    catalog_glue_region = env("OPENLAKEFORGE_CATALOG_GLUE_REGION", storage_region)
    catalog_token_uri = env(
        "OPENLAKEFORGE_CATALOG_TOKEN_URI",
        "http://polaris.olf-system:8181/api/catalog/v1/oauth/tokens",
    )
    catalog_warehouse = env("OPENLAKEFORGE_CATALOG_WAREHOUSE", env("OPENLAKEFORGE_CATALOG_NAME", "lakehouse_dev"))
    catalog_warehouse_prefix = env("OPENLAKEFORGE_CATALOG_WAREHOUSE_PREFIX", "")
    catalog_glue_database = env("OPENLAKEFORGE_CATALOG_GLUE_DATABASE", "")
    catalog_glue_warehouse_prefix = env("OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX", "warehouse/iceberg")
    catalog_scope = env("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE", "PRINCIPAL_ROLE:ALL")
    default_floe_image = "ghcr.io/malon64/floe:0.6.11"
    floe_image = env("FLOE_IMAGE", default_floe_image)
    storage_secret = env("OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME", "" if is_aws_s3 else "seaweedfs-s3-creds")
    storage_access_key = env("OPENLAKEFORGE_STORAGE_ACCESS_KEY_ID_KEY", "AWS_ACCESS_KEY_ID")
    storage_secret_key = env("OPENLAKEFORGE_STORAGE_SECRET_ACCESS_KEY_KEY", "AWS_SECRET_ACCESS_KEY")
    catalog_secret = env("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", "polaris-floe-creds")
    catalog_client_id_key = env("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY", "POLARIS_FLOE_CLIENT_ID")
    catalog_client_secret_key = env("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY", "POLARIS_FLOE_CLIENT_SECRET")
    governance_enabled = env("OPENLAKEFORGE_GOVERNANCE_ENABLED", "true") == "true"

    if storage_implementation.startswith("storage.") and "s3" in storage_implementation:
        catalog_warehouse_prefix = _absolute_s3_prefix(storage_silver_bucket, catalog_warehouse_prefix)
        catalog_glue_warehouse_prefix = _absolute_s3_prefix(storage_silver_bucket, catalog_glue_warehouse_prefix)

    runner_env = {
        "AWS_REGION": storage_region,
        "AWS_DEFAULT_REGION": storage_region,
        "AWS_S3_FORCE_PATH_STYLE": env(
            "OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS",
            "false" if is_aws_s3 else "true",
        ),
        "AWS_EC2_METADATA_DISABLED": "false" if is_aws_s3 else "true",
    }
    if env("OPENLAKEFORGE_STORAGE_SSL_MODE", "required" if is_aws_s3 else "disabled") == "disabled":
        runner_env["AWS_ALLOW_HTTP"] = "true"
    if storage_endpoint:
        runner_env["AWS_ENDPOINT_URL"] = storage_endpoint
    if storage_virtual_endpoint:
        # Floe's runner reads manifests and source objects from the local
        # S3-compatible store using the AWS SDK. In the local path-style setup,
        # the service endpoint must remain the concrete SeaweedFS S3 service,
        # not the bucket-virtual host base.
        runner_env["AWS_ENDPOINT_URL_S3"] = (
            storage_endpoint
            if env("OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS", "false" if is_aws_s3 else "true") == "true"
            else storage_virtual_endpoint
        )

    runner_env_yaml = "\n".join(f"      {key}: {_yaml_string(value)}" for key, value in runner_env.items())

    profile_variables = {
        "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": storage_bronze_bucket,
        "OPENLAKEFORGE_STORAGE_SILVER_BUCKET": storage_silver_bucket,
        "OPENLAKEFORGE_OPS_BUCKET_NAME": ops_bucket,
        "OPENLAKEFORGE_STORAGE_REGION": storage_region,
    }
    profile_variables_yaml = "\n".join(f"  {key}: {_yaml_string(value)}" for key, value in profile_variables.items())

    storage_secret_yaml = ""
    if storage_secret:
        storage_secret_yaml = f"""      - name: AWS_ACCESS_KEY_ID
        secret_name: {storage_secret}
        key: {storage_access_key}
      - name: AWS_SECRET_ACCESS_KEY
        secret_name: {storage_secret}
        key: {storage_secret_key}
"""

    catalog_secret_yaml = ""
    if catalog_type == "rest" and catalog_secret:
        catalog_secret_yaml = f"""      - name: OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID
        secret_name: {catalog_secret}
        key: {catalog_client_id_key}
      - name: OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET
        secret_name: {catalog_secret}
        key: {catalog_client_secret_key}
"""

    lineage_secret_yaml = ""
    lineage_block = ""
    if governance_enabled and env("OPENLINEAGE_DISABLED", "") != "true":
        lineage_url = env("OPENLINEAGE_URL", "http://openmetadata.olf-system:8585")
        lineage_endpoint = env("OPENLINEAGE_ENDPOINT", "api/v1/openlineage/lineage")
        lineage_namespace = env("OPENLINEAGE_NAMESPACE", "dagster")
        lineage_dataset_namespace = env(
            "OPENLAKEFORGE_FLOE_LINEAGE_DATASET_NAMESPACE",
            "aws_glue" if is_aws_glue else "polaris",
        )
        lineage_secret = env(
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
            "openmetadata-ingestion-bot",
        )
        lineage_secret_key = env(
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
            "OPENMETADATA_INGESTION_BOT_JWT",
        )
        lineage_secret_yaml = f"""      - name: OPENLINEAGE_API_KEY
        secret_name: {lineage_secret}
        key: {lineage_secret_key}
"""
        lineage_block = f"""lineage:
  url: {_yaml_string(lineage_url)}
  endpoint: {_yaml_string(lineage_endpoint)}
  namespace: {_yaml_string(lineage_namespace)}
  dataset_namespace: {_yaml_string(lineage_dataset_namespace)}
  timeout_secs: 5
"""

    secrets_entries = (storage_secret_yaml + catalog_secret_yaml + lineage_secret_yaml).rstrip("\n")
    if secrets_entries.strip():
        secrets_block = "    secrets:\n" + secrets_entries
    else:
        secrets_block = "    secrets: []"

    if catalog_type == "rest":
        catalog_definition = f"""    - name: "{catalog_name}"
      type: "rest"
      uri: "{catalog_rest_uri}"
      credential: "client_credentials:${{OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID}}:${{OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET}}"
      warehouse: "{catalog_warehouse}"
      oauth2_server_uri: "{catalog_token_uri}"
      scope: "{catalog_scope}"
      warehouse_storage: "lakehouse_silver"
      warehouse_prefix: "{catalog_warehouse_prefix}"
"""
    elif catalog_type == "glue" and catalog_provider == "aws-glue":
        if not catalog_glue_database:
            raise SystemExit(
                "ERROR: OPENLAKEFORGE_CATALOG_GLUE_DATABASE must be set to the target domain-scoped Glue database "
                "(for example sales_silver)."
            )
        catalog_definition = f"""    - name: "{catalog_name}"
      type: "glue"
      region: "{catalog_glue_region}"
      database: "{catalog_glue_database}"
      warehouse_storage: "lakehouse_silver"
      warehouse_prefix: "{catalog_glue_warehouse_prefix}"
      create_database_if_missing: false
"""
    else:
        raise SystemExit(f"ERROR: unsupported Floe catalog_type/provider: {catalog_type!r}/{catalog_provider!r}.")

    return f"""apiVersion: floe/v1
kind: EnvironmentProfile
metadata:
  name: {_yaml_string(profile_name)}
  env: {_yaml_string(profile_env)}
  description: {_yaml_string(profile_description)}
variables:
{profile_variables_yaml}
catalogs:
  default: "{catalog_name}"
  definitions:
{catalog_definition.rstrip()}
execution:
  orchestration:
    strategy: sequential
  runner:
    type: kubernetes_job
    image: {floe_image}
    namespace: {namespace}
    service_account: dagster
    timeout_seconds: 600
    ttl_seconds_after_finished: 3600
    poll_interval_seconds: 5
    env:
{runner_env_yaml}
{secrets_block}
{lineage_block}validation:
  strict: true
"""
