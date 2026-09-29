"""Provider-contract runtime environment resolution.

Single implementation of the logic that used to live in
scripts/local/contracts/load-runtime-env.sh (fallback defaults + provider
branching) and scripts/local/contracts/emit-contract-env.py (Terraform
`provider_contracts` output normalization). The shell wrapper evaluates the
`export`/`unset` lines this module prints, so exported variable names and
values must stay byte-identical for every provider.

Semantics preserved from the shell implementation:
- Defaults apply where a variable is unset OR empty (bash ``${VAR:-default}``).
- Every shared-service default names the namespace it runs in: a stage-scoped
  pod cannot resolve a bare service name in another namespace (ADR 0011).
- Terraform contract values override inherited environment values.
- After contract application, defaults run again to fill remaining gaps.
- Four variables honor a caller-set value even when contracts disagree:
  OPENLAKEFORGE_QUERY_SQLALCHEMY_URI, OPENMETADATA_CATALOG_SERVICE,
  OPENLAKEFORGE_STORAGE_OM_SERVICE, OPENLAKEFORGE_STORAGE_DISPLAY_NAME
  (bash ``${VAR+x}`` set-ness, which includes empty strings).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openlakeforge_domain import inventory_for

from olf.profile import DeploymentTopology, StageName
from olf.provider_contracts import (
    SUPPORTED_SCHEMA_VERSIONS,
    V2_SCHEMA_VERSION,
    ProviderContractError,
    parse_provider_contracts,
)
from olf.tooling.terraform import external_state_options

PROVIDER_CONTRACT_SCHEMA_VERSION = V2_SCHEMA_VERSION
PROVIDER_CONTRACTS_FILE_ENV = "OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE"


def load_provider_contracts(terraform_dir: str, *, environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Read the Terraform provider_contracts output, or None before apply.

    Returns the raw v2 or v3 payload; version dispatch belongs to
    `parse_provider_contracts`, and a v3 payload only resolves once its
    caller supplies a `DeploymentTopology` and selects a stage
    (`build_contract_env`). The local platform root still exports v2 -- #114
    switches it to v3 once every stage owns its storage and catalog.

    Only a missing executable (`ExecutableNotFoundError`) is treated as
    "not applied yet" - matching this function's pre-#127 behaviour, where
    `terraform` simply not being on `PATH` fell into the same `OSError`
    branch below. A managed-toolchain provisioning failure
    (`ToolchainError`: a bad digest, a broken download, a malformed
    catalog) is a real operational problem, not an unapplied-state signal,
    and must propagate rather than be silently treated as "no contracts
    yet" - a caller that swallowed it would fall back to defaults (e.g.
    enabling governance/analytics for what should be a slim deployment)
    instead of failing closed.

    With OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE set, the contract is read from
    that file instead (`olf platform contract` writes it): a CI job deploying
    a project revision to an applied platform has no access to the
    platform's Terraform state (#119). The file was named deliberately, so a
    missing or malformed one fails closed rather than reading as "not
    applied yet".
    """
    base_environ = environ if environ is not None else os.environ
    # Scoped provider envs drop ambient variables, so fall back to the process
    # env: this selector is set by the CI job, never by a provider.
    contract_file = base_environ.get(PROVIDER_CONTRACTS_FILE_ENV) or os.environ.get(PROVIDER_CONTRACTS_FILE_ENV)
    if contract_file:
        try:
            contracts = json.loads(Path(contract_file).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderContractError(f"{PROVIDER_CONTRACTS_FILE_ENV}={contract_file}: {exc}") from exc
        if not isinstance(contracts, dict):
            raise ProviderContractError(f"{PROVIDER_CONTRACTS_FILE_ENV}={contract_file}: not a JSON object")
    else:
        contracts = _terraform_provider_contracts(terraform_dir, environ=environ)
        if contracts is None:
            return None
    schema_version = contracts.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ProviderContractError(
            f"provider_contracts.schema_version {schema_version!r} is unsupported; "
            f"expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}"
        )
    return contracts


def _terraform_provider_contracts(terraform_dir: str, *, environ: Mapping[str, str] | None) -> dict[str, Any] | None:
    from olf.deployment.errors import ExecutableNotFoundError
    from olf.tooling.resolver import build_resolver

    try:
        resolver = build_resolver(environ=environ) if environ is not None else build_resolver()
        terraform = str(resolver.resolve("terraform"))
    except ExecutableNotFoundError:
        return None
    # An installed distribution's state lives under OLF_HOME, not next to
    # terraform_dir - without this, `terraform output` reads the read-only
    # payload's default (absent) state and this always returns None, even
    # after a successful `terraform apply` (see olf.tooling.terraform.
    # external_state_options, which Terraform._run also uses).
    base_environ = environ if environ is not None else os.environ
    overlay, state_path = external_state_options(Path(terraform_dir), base_environ, create=False)
    args = ["output", "-json", "provider_contracts"]
    if state_path is not None:
        args.insert(1, f"-state={state_path}")
    if state_path is not None:
        command_env: dict[str, str] | None = {**dict(base_environ), **overlay}
    elif environ is not None:
        command_env = dict(environ)
    else:
        command_env = None
    try:
        result = subprocess.run(
            [terraform, f"-chdir={terraform_dir}", *args],
            capture_output=True,
            text=True,
            check=True,
            env=command_env,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        contracts = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return contracts if isinstance(contracts, dict) else None


CONTRACT_STAGE_ENV = "OPENLAKEFORGE_CONTRACT_STAGE"


class _Env:
    """Mutable environment view that records exports and unsets."""

    def __init__(self, base: Mapping[str, str]):
        self._values = dict(base)
        self.exports: dict[str, str] = {}
        self.unsets: list[str] = []

    def get(self, name: str, fallback: str = "") -> str:
        value = self._values.get(name)
        return fallback if value is None or value == "" else value

    def raw(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        self._values[name] = value
        self.exports[name] = value
        if name in self.unsets:
            self.unsets.remove(name)

    def default(self, name: str, value: str) -> None:
        # bash ${NAME:-value}: unset or empty selects the default. The bash
        # scripts also re-export the existing value, which is a no-op for the
        # eval'ing shell, so only record a change when the default applies.
        current = self._values.get(name)
        if current is None or current == "":
            self.set(name, value)
        elif name not in self.exports:
            self.exports[name] = current

    def unset(self, name: str) -> None:
        self._values.pop(name, None)
        self.exports.pop(name, None)
        if name not in self.unsets:
            self.unsets.append(name)


def _contract_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _default_catalog_namespace_json(env: _Env, repo_root: Path) -> tuple[str, str, str]:
    """Render the three namespace JSON defaults from the domain inventory.

    Returns (catalog_namespaces_json, silver_namespaces_json, gold_namespaces_json).
    """
    physical = inventory_for(repo_root).resolve_physical_names(
        catalog_database_fqn="",
        bronze_bucket=env.get("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"),
        silver_bucket=env.get("OPENLAKEFORGE_STORAGE_SILVER_BUCKET"),
        gold_bucket=env.get("OPENLAKEFORGE_STORAGE_GOLD_BUCKET"),
        manifest_base_uri="",
    )
    catalog_namespaces_json = json.dumps(
        [{"name": namespace.name, "location": namespace.location} for namespace in physical.catalog_namespaces],
        separators=(",", ":"),
    )
    silver_namespaces_json = json.dumps(physical.silver_namespaces, separators=(",", ":"))
    gold_namespaces_json = json.dumps(physical.gold_namespaces, separators=(",", ":"))
    return catalog_namespaces_json, silver_namespaces_json, gold_namespaces_json


def _apply_default_contract_env(env: _Env, base: Mapping[str, str], repo_root: Path) -> None:
    env.default("OPENLAKEFORGE_STORAGE_LOGICAL_NAME", "lakehouse_storage")
    env.default("OPENLAKEFORGE_STORAGE_PROVIDER", "local")
    env.default("OPENLAKEFORGE_STORAGE_IMPLEMENTATION", "storage.s3_compatible.seaweedfs")
    env.default("OPENLAKEFORGE_STORAGE_TYPE", "s3")
    env.default("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET", "lakehouse-bronze")
    env.default("OPENLAKEFORGE_STORAGE_SILVER_BUCKET", "lakehouse-silver")
    env.default("OPENLAKEFORGE_STORAGE_GOLD_BUCKET", "lakehouse-gold")
    env.default("OPENLAKEFORGE_STORAGE_BUCKET", env.get("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"))
    env.default("OPENLAKEFORGE_STORAGE_REGION", "us-east-1")
    env.default("OPENLAKEFORGE_STORAGE_ENDPOINT", "http://seaweedfs-s3.olf-system:8333")
    env.default("OPENLAKEFORGE_STORAGE_VIRTUAL_HOST_ENDPOINT", "http://olf-system.svc.cluster.local:8333")
    env.default("OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS", "true")
    env.default("OPENLAKEFORGE_STORAGE_SSL_MODE", "disabled")
    env.default("OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME", "seaweedfs-s3-creds")
    env.default("OPENLAKEFORGE_STORAGE_ACCESS_KEY_ID_KEY", "AWS_ACCESS_KEY_ID")
    env.default("OPENLAKEFORGE_STORAGE_SECRET_ACCESS_KEY_KEY", "AWS_SECRET_ACCESS_KEY")
    env.default("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAME", "seaweedfs-s3")
    env.default("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAMESPACE", "olf-system")
    env.default("OPENLAKEFORGE_STORAGE_S3_SERVICE_PORT", "8333")

    env.default("OPENLAKEFORGE_CATALOG_LOGICAL_NAME", "iceberg_catalog")
    env.default("OPENLAKEFORGE_CATALOG_IMPLEMENTATION", "catalog.iceberg_rest.polaris")
    env.default("OPENLAKEFORGE_CATALOG_TYPE", "rest")
    env.default("OPENLAKEFORGE_CATALOG_PROVIDER", "polaris")
    env.default("OPENLAKEFORGE_CATALOG_NAME", "lakehouse_dev")
    env.default("OPENLAKEFORGE_CATALOG_RUNTIME_PROFILE", "polaris-rest")
    env.default("OPENLAKEFORGE_CATALOG_REST_URI", "http://polaris.olf-system:8181/api/catalog")
    env.default("OPENLAKEFORGE_CATALOG_TOKEN_URI", "http://polaris.olf-system:8181/api/catalog/v1/oauth/tokens")
    env.default("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE", "PRINCIPAL_ROLE:ALL")
    env.default("OPENLAKEFORGE_CATALOG_WAREHOUSE", "lakehouse_dev")
    env.default("OPENLAKEFORGE_CATALOG_GLUE_REGION", "")
    env.default("OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID", "")
    env.default("OPENLAKEFORGE_CATALOG_GLUE_REST_URI", "")
    env.default("OPENLAKEFORGE_CATALOG_GLUE_REST_WAREHOUSE", env.get("OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID"))
    env.default("OPENLAKEFORGE_CATALOG_GLUE_DATABASE", "")
    env.default("OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX", "warehouse/iceberg")
    env.default("OPENLAKEFORGE_CATALOG_NAMESPACE_MODEL", "medallion-owner")
    default_catalog_namespaces_json, default_silver_namespaces_json, default_gold_namespaces_json = (
        _default_catalog_namespace_json(env, repo_root)
    )
    env.default("OPENLAKEFORGE_CATALOG_NAMESPACES_JSON", default_catalog_namespaces_json)
    env.default("OPENLAKEFORGE_CATALOG_SILVER_NAMESPACES_JSON", default_silver_namespaces_json)
    env.default("OPENLAKEFORGE_CATALOG_GOLD_NAMESPACES_JSON", default_gold_namespaces_json)
    env.default("OPENLAKEFORGE_CATALOG_SILVER_NAMESPACE", "")
    env.default("OPENLAKEFORGE_CATALOG_GOLD_NAMESPACE", "")
    env.default("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", "polaris-floe-creds")
    env.default("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY", "POLARIS_FLOE_CLIENT_ID")
    env.default("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY", "POLARIS_FLOE_CLIENT_SECRET")
    env.default("OPENLAKEFORGE_CATALOG_DEPLOYER_CREDENTIALS_SECRET_NAME", "polaris-deployer-creds")
    env.default("OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_ID_KEY", "POLARIS_DEPLOYER_CLIENT_ID")
    env.default("OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_SECRET_KEY", "POLARIS_DEPLOYER_CLIENT_SECRET")

    env.default(
        "OPENLAKEFORGE_OPS_BUCKET_NAME",
        env.get("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME", "openlakeforge-ops"),
    )
    env.default("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME", env.get("OPENLAKEFORGE_OPS_BUCKET_NAME"))
    env.default(
        "OPENLAKEFORGE_ARTIFACT_BASE_URI",
        f"s3://{env.get('OPENLAKEFORGE_ARTIFACT_BUCKET_NAME')}",
    )
    env.default("OPENLAKEFORGE_FLOE_MANIFEST_ACCESS_MODE", "remote")
    env.default(
        "OPENLAKEFORGE_FLOE_MANIFEST_BASE_URI",
        f"{env.get('OPENLAKEFORGE_ARTIFACT_BASE_URI')}/floe/manifests",
    )
    env.default(
        "OPENLAKEFORGE_FLOE_REPORT_BASE_URI",
        f"{env.get('OPENLAKEFORGE_ARTIFACT_BASE_URI')}/floe/reports",
    )
    env.default("OPENLAKEFORGE_LOG_BASE_URI", f"{env.get('OPENLAKEFORGE_ARTIFACT_BASE_URI')}/logs")
    env.default(
        "OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI",
        f"{env.get('OPENLAKEFORGE_ARTIFACT_BASE_URI')}/run-artifacts",
    )
    env.default("OPENLAKEFORGE_ARTIFACT_LOCAL_UPLOAD_ACCESS_MODE", "kubectl-port-forward")
    # The shared admin storage identity used by olf's own revision-publish/
    # manifest-upload tooling (see the matching OPENLAKEFORGE_STORAGE_*
    # defaults above) - distinct from a stage's own storage identity once a
    # native v3 contract applies.
    env.default("OPENLAKEFORGE_OPS_STORAGE_CREDENTIALS_SECRET_NAME", "seaweedfs-s3-creds")
    env.default("OPENLAKEFORGE_OPS_STORAGE_ACCESS_KEY_ID_KEY", "AWS_ACCESS_KEY_ID")
    env.default("OPENLAKEFORGE_OPS_STORAGE_SECRET_ACCESS_KEY_KEY", "AWS_SECRET_ACCESS_KEY")
    env.default("OPENLAKEFORGE_QUERY_TRINO_HOST", "trino.olf-system")
    env.default("OPENLAKEFORGE_QUERY_TRINO_PORT", "8080")
    env.default("OPENLAKEFORGE_QUERY_TRINO_CATALOG", "iceberg")

    env.default("OPENLAKEFORGE_KUBE_NAMESPACE", env.get("NAMESPACE", "olf-dev"))

    env.default("AWS_REGION", env.get("OPENLAKEFORGE_STORAGE_REGION"))
    env.default("AWS_DEFAULT_REGION", env.get("AWS_REGION"))
    if env.get("OPENLAKEFORGE_STORAGE_IMPLEMENTATION") == "storage.aws_s3":
        env.unset("AWS_ENDPOINT_URL_S3")
        env.default("AWS_S3_FORCE_PATH_STYLE", "false")
    else:
        env.default("AWS_ENDPOINT_URL_S3", env.get("OPENLAKEFORGE_STORAGE_ENDPOINT"))
        env.default("AWS_S3_FORCE_PATH_STYLE", env.get("OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS"))
    if env.get("OPENLAKEFORGE_STORAGE_SSL_MODE") == "disabled":
        env.default("AWS_ALLOW_HTTP", "true")

    # Compatibility names for current Polaris REST consumers.
    env.default("POLARIS_REST_URI", env.get("OPENLAKEFORGE_CATALOG_REST_URI"))
    env.default("POLARIS_TOKEN_URI", env.get("OPENLAKEFORGE_CATALOG_TOKEN_URI"))
    env.default("POLARIS_WAREHOUSE", env.get("OPENLAKEFORGE_CATALOG_WAREHOUSE"))
    env.default("POLARIS_OAUTH_SCOPE", env.get("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE"))
    env.default("OPENLAKEFORGE_DBT_TRINO_USER", "openlakeforge-dbt")
    env.default("OPENLAKEFORGE_DBT_EXECUTABLE", "dbt-ol")
    env.default("OPENLAKEFORGE_GOVERNANCE_ENABLED", "true")
    env.default("OPENLAKEFORGE_ANALYTICS_ENABLED", "true")
    if env.get("OPENLAKEFORGE_GOVERNANCE_ENABLED") == "true":
        env.default("OPENLINEAGE_URL", "http://openmetadata.olf-system:8585")
        env.default("OPENLINEAGE_ENDPOINT", "api/v1/openlineage/lineage")
        env.default("OPENLINEAGE_NAMESPACE", "dagster")
        env.default(
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
            "openmetadata-ingestion-bot",
        )
        env.default(
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
            "OPENMETADATA_INGESTION_BOT_JWT",
        )
    else:
        for name in (
            "OPENLINEAGE_URL",
            "OPENLINEAGE_ENDPOINT",
            "OPENLINEAGE_NAMESPACE",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
        ):
            env.unset(name)


def _apply_provider_contracts(env: _Env, contracts: dict[str, Any]) -> None:
    storage = contracts.get("storage") or {}
    catalog = contracts.get("catalog") or {}
    artifact_bucket = contracts.get("artifact_bucket", contracts.get("artifacts")) or {}
    platform = contracts.get("kubernetes_platform", contracts.get("cluster")) or {}
    query = contracts.get("query") or {}
    governance = contracts.get("governance") or {}
    reporting = contracts.get("reporting") or {}
    is_glue_catalog = catalog.get("catalog_type") == "glue" and catalog.get("catalog_provider") == "aws-glue"
    catalog_warehouse = (
        catalog.get("catalog_name") if is_glue_catalog else (catalog.get("warehouse") or catalog.get("catalog_name"))
    )

    def emit(name: str, value: Any) -> None:
        if value is None:
            return
        env.set(name, _contract_value(value))

    def emit_json(name: str, value: Any) -> None:
        if value is None:
            return
        env.set(name, json.dumps(value, separators=(",", ":")))

    emit("OPENLAKEFORGE_STORAGE_LOGICAL_NAME", storage.get("logical_name"))
    emit("OPENLAKEFORGE_STORAGE_PROVIDER", storage.get("provider"))
    emit("OPENLAKEFORGE_STORAGE_IMPLEMENTATION", storage.get("implementation"))
    emit("OPENLAKEFORGE_STORAGE_TYPE", storage.get("protocol"))
    emit("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET", storage.get("bronze_bucket_name"))
    emit("OPENLAKEFORGE_STORAGE_SILVER_BUCKET", storage.get("silver_bucket_name"))
    emit("OPENLAKEFORGE_STORAGE_GOLD_BUCKET", storage.get("gold_bucket_name"))
    emit("OPENLAKEFORGE_STORAGE_BUCKET", storage.get("bucket_name"))
    emit("OPENLAKEFORGE_STORAGE_REGION", storage.get("region"))
    emit("OPENLAKEFORGE_STORAGE_ENDPOINT", storage.get("endpoint"))
    emit("OPENLAKEFORGE_STORAGE_VIRTUAL_HOST_ENDPOINT", storage.get("virtual_host_endpoint"))
    emit("OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS", storage.get("path_style_access"))
    emit("OPENLAKEFORGE_STORAGE_SSL_MODE", storage.get("ssl_mode"))
    emit("OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME", storage.get("credentials_secret_name"))
    emit("OPENLAKEFORGE_STORAGE_ACCESS_KEY_ID_KEY", storage.get("access_key_id_key"))
    emit("OPENLAKEFORGE_STORAGE_SECRET_ACCESS_KEY_KEY", storage.get("secret_access_key_key"))
    emit("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAME", storage.get("s3_service_name"))
    emit("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAMESPACE", storage.get("s3_service_namespace"))
    emit("OPENLAKEFORGE_STORAGE_S3_SERVICE_PORT", storage.get("s3_service_port"))

    emit("OPENLAKEFORGE_CATALOG_LOGICAL_NAME", catalog.get("logical_name"))
    emit("OPENLAKEFORGE_CATALOG_IMPLEMENTATION", catalog.get("implementation"))
    emit("OPENLAKEFORGE_CATALOG_TYPE", catalog.get("catalog_type"))
    emit("OPENLAKEFORGE_CATALOG_PROVIDER", catalog.get("catalog_provider"))
    emit("OPENLAKEFORGE_CATALOG_NAME", catalog.get("catalog_name"))
    emit("OPENLAKEFORGE_CATALOG_RUNTIME_PROFILE", catalog.get("runtime_profile"))
    emit("OPENLAKEFORGE_CATALOG_REST_URI", catalog.get("rest_uri"))
    emit("OPENLAKEFORGE_CATALOG_TOKEN_URI", catalog.get("token_uri"))
    emit("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE", catalog.get("oauth_scope"))
    emit("OPENLAKEFORGE_CATALOG_WAREHOUSE", catalog_warehouse)
    emit("OPENLAKEFORGE_CATALOG_GLUE_REGION", catalog.get("glue_region"))
    emit("OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID", catalog.get("glue_catalog_id"))
    emit("OPENLAKEFORGE_CATALOG_GLUE_REST_URI", catalog.get("glue_rest_uri") or catalog.get("rest_uri"))
    emit(
        "OPENLAKEFORGE_CATALOG_GLUE_REST_WAREHOUSE",
        catalog.get("glue_rest_warehouse") or catalog.get("warehouse"),
    )
    emit("OPENLAKEFORGE_CATALOG_GLUE_DATABASE", catalog.get("glue_database"))
    emit("OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX", catalog.get("glue_warehouse_prefix"))
    emit("OPENLAKEFORGE_CATALOG_NAMESPACE_MODEL", catalog.get("catalog_namespace_model"))
    emit_json("OPENLAKEFORGE_CATALOG_NAMESPACES_JSON", catalog.get("catalog_namespaces"))
    emit_json("OPENLAKEFORGE_CATALOG_SILVER_NAMESPACES_JSON", catalog.get("silver_namespaces"))
    emit_json("OPENLAKEFORGE_CATALOG_GOLD_NAMESPACES_JSON", catalog.get("gold_namespaces"))
    emit("OPENLAKEFORGE_CATALOG_DATABASE_FQN", catalog.get("catalog_database_fqn"))
    emit_json("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON", catalog.get("silver_schema_fqns"))
    emit_json("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", catalog.get("gold_schema_fqns"))
    emit("OPENLAKEFORGE_CATALOG_SILVER_NAMESPACE", catalog.get("silver_namespace"))
    emit("OPENLAKEFORGE_CATALOG_GOLD_NAMESPACE", catalog.get("gold_namespace"))
    emit(
        "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME",
        catalog.get("floe_credentials_secret_name"),
    )
    emit("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY", catalog.get("floe_client_id_key"))
    emit("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY", catalog.get("floe_client_secret_key"))
    emit(
        "OPENLAKEFORGE_CATALOG_DEPLOYER_CREDENTIALS_SECRET_NAME",
        catalog.get("deployer_credentials_secret_name"),
    )
    emit("OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_ID_KEY", catalog.get("deployer_client_id_key"))
    emit("OPENLAKEFORGE_CATALOG_DEPLOYER_CLIENT_SECRET_KEY", catalog.get("deployer_client_secret_key"))

    ops_bucket = artifact_bucket.get("bucket_name") or artifact_bucket.get("ops_bucket_name")
    emit("OPENLAKEFORGE_OPS_BUCKET_NAME", ops_bucket)
    emit("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME", ops_bucket)
    emit("OPENLAKEFORGE_ARTIFACT_BASE_URI", artifact_bucket.get("artifact_base_uri"))
    emit(
        "OPENLAKEFORGE_FLOE_MANIFEST_ACCESS_MODE",
        artifact_bucket.get("access_mode") or artifact_bucket.get("floe_manifest_access_mode"),
    )
    emit(
        "OPENLAKEFORGE_FLOE_MANIFEST_BASE_URI",
        artifact_bucket.get("base_uri") or artifact_bucket.get("floe_manifest_base_uri"),
    )
    emit("OPENLAKEFORGE_FLOE_REPORT_BASE_URI", artifact_bucket.get("floe_report_base_uri"))
    emit("OPENLAKEFORGE_LOG_BASE_URI", artifact_bucket.get("log_base_uri"))
    emit("OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI", artifact_bucket.get("run_artifact_base_uri"))
    emit(
        "OPENLAKEFORGE_ARTIFACT_LOCAL_UPLOAD_ACCESS_MODE",
        artifact_bucket.get("local_upload_access_mode"),
    )
    # The shared admin storage identity - distinct from
    # OPENLAKEFORGE_STORAGE_* (that stage's own, narrower identity): olf's
    # own revision-publish/manifest-upload tooling writes to
    # floe/revisions/... at the ops bucket root, outside any stage's
    # activations/<stage> prefix, so it needs the admin identity's own
    # Secret, read from the shared namespace (see
    # artifact_store.artifact_storage_client).
    emit("OPENLAKEFORGE_OPS_STORAGE_CREDENTIALS_SECRET_NAME", artifact_bucket.get("credentials_secret_name"))
    emit("OPENLAKEFORGE_OPS_STORAGE_ACCESS_KEY_ID_KEY", artifact_bucket.get("access_key_id_key"))
    emit("OPENLAKEFORGE_OPS_STORAGE_SECRET_ACCESS_KEY_KEY", artifact_bucket.get("secret_access_key_key"))
    emit("OPENLAKEFORGE_KUBE_NAMESPACE", platform.get("namespace"))
    emit("OPENLAKEFORGE_QUERY_TRINO_CATALOG", query.get("catalog_name"))
    emit("OPENLAKEFORGE_DBT_TRINO_USER", query.get("runtime_identity_principal"))
    governance_enabled = governance.get("enabled", True)
    emit("OPENLAKEFORGE_GOVERNANCE_ENABLED", governance_enabled)
    emit("OPENLAKEFORGE_ANALYTICS_ENABLED", reporting.get("enabled", True))
    if governance_enabled:
        emit("OPENLINEAGE_URL", governance.get("endpoint"))
        emit(
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
            governance.get("ingestion_bot_secret_name"),
        )
        emit(
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
            governance.get("ingestion_bot_jwt_key"),
        )
    else:
        for name in (
            "OPENLINEAGE_URL",
            "OPENLINEAGE_ENDPOINT",
            "OPENLINEAGE_NAMESPACE",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
            "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
        ):
            env.unset(name)
    endpoint = query.get("endpoint")
    if endpoint and endpoint.startswith("http://"):
        host_port = endpoint.removeprefix("http://").split("/", 1)[0]
        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            emit("OPENLAKEFORGE_QUERY_TRINO_HOST", host)
            emit("OPENLAKEFORGE_QUERY_TRINO_PORT", port)


def build_contract_env(
    base: Mapping[str, str],
    contracts: Mapping[str, Any] | None,
    *,
    repo_root: Path,
    topology: DeploymentTopology | None = None,
    stage: StageName | str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Compute the runtime contract environment.

    Returns (exports, unsets): the variables to export (insertion-ordered)
    and the variables to unset in the caller's shell.
    """
    query_uri_user_set = "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI" in base
    om_catalog_service_user_set = "OPENMETADATA_CATALOG_SERVICE" in base
    storage_om_service_user_set = "OPENLAKEFORGE_STORAGE_OM_SERVICE" in base
    storage_display_name_user_set = "OPENLAKEFORGE_STORAGE_DISPLAY_NAME" in base
    aws_region_user_set = "AWS_REGION" in base
    aws_default_region_user_set = "AWS_DEFAULT_REGION" in base

    env = _Env(base)
    _apply_default_contract_env(env, base, repo_root)
    native_v3 = False
    if contracts is not None:
        parsed = parse_provider_contracts(contracts, topology)
        native_v3 = not parsed.compatibility_v2
        if parsed.compatibility_v2 and stage is not None and StageName(stage) != StageName.DEV:
            # The v2 payload carries one stage's bindings with no stage index,
            # so there is nothing to select: returning it for another stage
            # would hand that stage DEV's namespace, catalog, and capability
            # flags while reporting success. #114 emits v3, which can answer
            # this properly.
            raise ProviderContractError(
                f"provider contract v2 describes only the DEV stage; it cannot serve {StageName(stage).value!r} "
                "(#114 switches the roots to v3)"
            )
        resolved_contract = (
            dict(contracts) if parsed.compatibility_v2 else parsed.for_stage(stage).as_v2_environment_contract()
        )
        _apply_provider_contracts(env, resolved_contract)
        _apply_default_contract_env(env, base, repo_root)
        # Provenance, not a binding: which stage an applied contract actually
        # served. Set here and unset below, never defaulted, so a value left
        # in the caller's shell cannot outlive a run whose Terraform output
        # was unavailable.
        env.set(CONTRACT_STAGE_ENV, StageName(stage).value if stage is not None else StageName.DEV.value)
    elif env.raw(CONTRACT_STAGE_ENV) is not None:
        env.unset(CONTRACT_STAGE_ENV)
    if env.get("OPENLAKEFORGE_STORAGE_IMPLEMENTATION") == "storage.aws_s3":
        env.set("OPENLAKEFORGE_STORAGE_ENDPOINT", "")
        env.set("OPENLAKEFORGE_STORAGE_VIRTUAL_HOST_ENDPOINT", "")
        env.set("OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS", "false")
        env.set("OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME", "")
        env.set("OPENLAKEFORGE_STORAGE_ACCESS_KEY_ID_KEY", "")
        env.set("OPENLAKEFORGE_STORAGE_SECRET_ACCESS_KEY_KEY", "")
        # AWS S3 authenticates through Pod Identity, not a static Secret -
        # olf's own admin upload path (direct via boto3, never port-forward)
        # needs none either.
        env.set("OPENLAKEFORGE_OPS_STORAGE_CREDENTIALS_SECRET_NAME", "")
        env.set("OPENLAKEFORGE_OPS_STORAGE_ACCESS_KEY_ID_KEY", "")
        env.set("OPENLAKEFORGE_OPS_STORAGE_SECRET_ACCESS_KEY_KEY", "")
        env.set("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAME", "")
        env.set("OPENLAKEFORGE_STORAGE_S3_SERVICE_NAMESPACE", "")
        env.set("OPENLAKEFORGE_STORAGE_S3_SERVICE_PORT", "")
        env.unset("AWS_ENDPOINT_URL_S3")
        env.default("AWS_S3_FORCE_PATH_STYLE", "false")

    is_glue = (
        env.get("OPENLAKEFORGE_CATALOG_TYPE") == "glue" and env.get("OPENLAKEFORGE_CATALOG_PROVIDER") == "aws-glue"
    )
    if is_glue:
        env.default("OPENLAKEFORGE_CATALOG_GLUE_DATABASE", "")
        env.set("OPENLAKEFORGE_CATALOG_WAREHOUSE", env.get("OPENLAKEFORGE_CATALOG_NAME"))
        env.default(
            "OPENLAKEFORGE_CATALOG_GLUE_REST_WAREHOUSE",
            env.get("OPENLAKEFORGE_CATALOG_GLUE_CATALOG_ID"),
        )
        env.default("OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX", "warehouse/iceberg")
        env.set("OPENLAKEFORGE_CATALOG_TOKEN_URI", "")
        env.set("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE", "")
        env.set("OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME", "")
        env.set("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY", "")
        env.set("OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY", "")

    if native_v3:
        # env.default() (used for these four names in _apply_default_contract_env)
        # only fills an unset-or-empty value: the first default pass above already
        # pinned POLARIS_WAREHOUSE etc. to the OPENLAKEFORGE_CATALOG_* dev defaults
        # before the stage's real values were applied, so an intermediate default
        # pass leaves them stale. env.set() here re-derives all four from the
        # now-fully-resolved (including the is_glue normalization above, which
        # blanks OPENLAKEFORGE_CATALOG_TOKEN_URI/OAUTH_SCOPE) stage values,
        # unconditionally overriding any caller-set value — unlike the four
        # OPENLAKEFORGE_* names that honor caller set-ness (see module
        # docstring), these compatibility aliases always mirror the resolved
        # stage. This must run after the is_glue block: reading
        # OPENLAKEFORGE_CATALOG_TOKEN_URI/OAUTH_SCOPE before Glue normalization
        # blanks them would leave POLARIS_TOKEN_URI/OAUTH_SCOPE pinned to the
        # local Polaris defaults for a Glue stage.
        env.set("POLARIS_REST_URI", env.get("OPENLAKEFORGE_CATALOG_REST_URI"))
        env.set("POLARIS_TOKEN_URI", env.get("OPENLAKEFORGE_CATALOG_TOKEN_URI"))
        env.set("POLARIS_WAREHOUSE", env.get("OPENLAKEFORGE_CATALOG_WAREHOUSE"))
        env.set("POLARIS_OAUTH_SCOPE", env.get("OPENLAKEFORGE_CATALOG_OAUTH_SCOPE"))
        # Same staleness as the POLARIS_* aliases above, for the non-aws_s3
        # branch's AWS_ENDPOINT_URL_S3/AWS_S3_FORCE_PATH_STYLE: the first
        # default pass already pinned them from the generic local endpoint
        # default before the stage's real storage.endpoint/path_style_access
        # applied, and env.default() (used for both names below) will not
        # move an already-set value. A stage with a non-default endpoint
        # would otherwise leave consumers like libs/bronze_csv.py connecting
        # to the wrong SeaweedFS host or using the wrong addressing style.
        # The storage.aws_s3 block above already re-derives both correctly
        # (unconditional unset/default run after the resolved implementation
        # is known), so this only needs to cover the non-aws_s3 case.
        if env.get("OPENLAKEFORGE_STORAGE_IMPLEMENTATION") != "storage.aws_s3":
            env.set("AWS_ENDPOINT_URL_S3", env.get("OPENLAKEFORGE_STORAGE_ENDPOINT"))
            env.set("AWS_S3_FORCE_PATH_STYLE", env.get("OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS"))

    catalog_om_service_name = "aws_glue" if is_glue else "polaris"
    catalog_name = env.get("OPENLAKEFORGE_CATALOG_NAME")
    database_fqn = f"{catalog_om_service_name}.{catalog_name}"
    # AWS Glue has no per-stage catalog (this account's Glue service refuses
    # to create custom/"native" catalogs) - every stage shares the account's
    # one default catalog, so its physical database names need a per-stage
    # prefix to stay collision-free. Every other provider still gets a
    # per-stage catalog, so the catalog boundary alone is enough there.
    schema_fqn_physical = inventory_for(repo_root).resolve_physical_names(
        catalog_database_fqn=database_fqn,
        bronze_bucket=env.get("OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"),
        silver_bucket=env.get("OPENLAKEFORGE_STORAGE_SILVER_BUCKET"),
        gold_bucket=env.get("OPENLAKEFORGE_STORAGE_GOLD_BUCKET"),
        manifest_base_uri=env.get("OPENLAKEFORGE_FLOE_MANIFEST_BASE_URI"),
        namespace_prefix=f"{catalog_name}_" if is_glue else "",
    )
    default_silver_fqns = json.dumps(schema_fqn_physical.silver_schema_fqns, separators=(",", ":"))
    default_gold_fqns = json.dumps(schema_fqn_physical.gold_schema_fqns, separators=(",", ":"))
    if env.get("OPENLAKEFORGE_CATALOG_DATABASE_FQN") == "":
        env.set("OPENLAKEFORGE_CATALOG_DATABASE_FQN", database_fqn)
    if env.get("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON") == "":
        env.set("OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON", default_silver_fqns)
    if env.get("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON") == "":
        env.set("OPENLAKEFORGE_CATALOG_GOLD_SCHEMA_FQNS_JSON", default_gold_fqns)
    # The Terraform contract's own namespace JSON fields (emitted above, from
    # `catalog.{catalog,silver,gold}_namespaces`) describe *logical*
    # namespaces and carry no namespace_prefix - Terraform has no notion of
    # this account's shared-default-catalog workaround. They are therefore
    # never actually empty by this point (unlike `_apply_default_contract_
    # env`'s early bootstrap defaults), so an empty-check here would never
    # fire and the bare, unprefixed names would reach Floe's AWS profile
    # rendering and `olf catalog sync-namespaces`, colliding a Glue stage's
    # database with every other Glue stage's identically-named one (and
    # Glue would deny access to the physical, prefixed database that
    # actually exists). Override unconditionally for Glue; keep the
    # empty-check as a fallback default for every other provider.
    if is_glue or env.get("OPENLAKEFORGE_CATALOG_NAMESPACES_JSON") == "":
        env.set(
            "OPENLAKEFORGE_CATALOG_NAMESPACES_JSON",
            json.dumps(
                [
                    {"name": namespace.name, "location": namespace.location}
                    for namespace in schema_fqn_physical.catalog_namespaces
                ],
                separators=(",", ":"),
            ),
        )
    if is_glue or env.get("OPENLAKEFORGE_CATALOG_SILVER_NAMESPACES_JSON") == "":
        env.set(
            "OPENLAKEFORGE_CATALOG_SILVER_NAMESPACES_JSON",
            json.dumps(schema_fqn_physical.silver_namespaces, separators=(",", ":")),
        )
    if is_glue or env.get("OPENLAKEFORGE_CATALOG_GOLD_NAMESPACES_JSON") == "":
        env.set(
            "OPENLAKEFORGE_CATALOG_GOLD_NAMESPACES_JSON",
            json.dumps(schema_fqn_physical.gold_namespaces, separators=(",", ":")),
        )
    # dbt's per-product Gold profile template (libs/dbt/profiles/aws.yml)
    # bakes its bare `{{GOLD_SCHEMA}}` (e.g. "order_revenue_gold") at
    # project-code image BUILD time - the image is provider/stage-agnostic
    # ("one image digest deploys to every stage"), so it cannot know this
    # account's shared-catalog namespace_prefix. Prepend it at dbt-invoke
    # time instead, via this runtime-only env var the template reads with
    # `env_var(...)` alongside its build-time-baked schema name.
    env.set("OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX", f"{catalog_name}_" if is_glue else "")
    if not om_catalog_service_user_set:
        env.set("OPENMETADATA_CATALOG_SERVICE", catalog_om_service_name)
    if env.get("OPENLAKEFORGE_STORAGE_IMPLEMENTATION") == "storage.aws_s3":
        if not storage_om_service_user_set:
            env.set("OPENLAKEFORGE_STORAGE_OM_SERVICE", "aws_s3")
        if not storage_display_name_user_set:
            env.set("OPENLAKEFORGE_STORAGE_DISPLAY_NAME", "AWS S3")
    else:
        if not storage_om_service_user_set:
            env.set("OPENLAKEFORGE_STORAGE_OM_SERVICE", "seaweedfs")
        if not storage_display_name_user_set:
            env.set("OPENLAKEFORGE_STORAGE_DISPLAY_NAME", "SeaweedFS S3")

    # Re-derive AWS_REGION from the *resolved* storage region. The first default
    # pass runs before provider contracts apply (storage region still at the
    # us-east-1 default), and the resolver runs in a subprocess that cannot see
    # a shell-local AWS_REGION, so without this a cloud stack would keep the
    # stale default. A caller that exported AWS_REGION still wins.
    if not aws_region_user_set:
        env.set("AWS_REGION", env.get("OPENLAKEFORGE_STORAGE_REGION"))
    if not aws_default_region_user_set:
        env.set("AWS_DEFAULT_REGION", env.get("AWS_REGION"))

    env.default("CODE_BUCKET_NAME", env.get("OPENLAKEFORGE_ARTIFACT_BUCKET_NAME"))
    env.default("OPENMETADATA_CATALOG_DATABASE", env.get("OPENLAKEFORGE_CATALOG_NAME"))
    if not query_uri_user_set:
        env.set(
            "OPENLAKEFORGE_QUERY_SQLALCHEMY_URI",
            "trino://{user}@{host}:{port}/{catalog}".format(
                # Reuse the stage's own runtime identity: Trino's file-based
                # access control (modules/query/trino/main.tf) grants
                # catalog access by this exact user string, and a literal
                # "superset" would be denied by the default-deny catalog rule.
                user=env.get("OPENLAKEFORGE_DBT_TRINO_USER"),
                host=env.get("OPENLAKEFORGE_QUERY_TRINO_HOST"),
                port=env.get("OPENLAKEFORGE_QUERY_TRINO_PORT"),
                catalog=env.get("OPENLAKEFORGE_QUERY_TRINO_CATALOG"),
            ),
        )

    return env.exports, env.unsets


def render_shell_exports(exports: Mapping[str, str], unsets: list[str]) -> str:
    lines = [f"export {name}={shlex.quote(value)}" for name, value in exports.items()]
    lines.extend(f"unset {name}" for name in unsets)
    return "\n".join(lines)
