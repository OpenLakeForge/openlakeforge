"""The runtime contract between the platform and the project-code image.

A ProjectRevision is built once and promoted unchanged (ADR 0012), so it must
survive a platform patch release. What it depends on is not the distribution
version but this contract: the environment the platform hands the code server
and the path it mounts. `release/component-catalog.yaml` carries an integer
contract version, the versions this distribution still accepts, and a
fingerprint of the surface below. `olf release check` fails when the surface
changes and the fingerprint does not, so a change here cannot ship without a
deliberate version decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Mounted read-only into the code server when the query endpoint is TLS.
QUERY_CA_DIR = "/etc/olf/query-ca"

# Contract-derived runtime settings that deliberately carry no OPENLAKEFORGE_
# prefix because the libraries reading them are not ours: openlakeforge_runtime.s3_artifacts
# resolves the object store through AWS_ENDPOINT_URL_S3, so a local or Azure
# stage that loses these reaches public S3 instead of its contracted endpoint.
# Terraform passed exactly this set to the code server. Credential *values*
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, OPENLINEAGE_API_KEY) stay out --
# those arrive by Secret reference.
RUNTIME_ALIASES = (
    "AWS_ALLOW_HTTP",
    "AWS_DEFAULT_REGION",
    "AWS_ENDPOINT_URL_S3",
    "AWS_REGION",
    "AWS_S3_FORCE_PATH_STYLE",
    "OPENLINEAGE_ENDPOINT",
    "OPENLINEAGE_NAMESPACE",
    "OPENLINEAGE_URL",
)
# The stage contract values the project-code runtime reads (openlakeforge_runtime/, the dbt
# profile and projects, lakehouse_code/), and nothing else. Forwarding every
# OPENLAKEFORGE_* name in the operator's process also shipped workstation
# paths, deployer Secret names and settings no workload reads (#546). The
# *_SECRET_NAME and *_KEY names are Secret and key names the Floe Job spec
# references, never credential values. tests/infra/test_deployment_activation.py
# fails when runtime code reads a name this list does not account for.
RUNTIME_ENV = (
    "OPENLAKEFORGE_ARTIFACT_BASE_URI",
    "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_ID_KEY",
    "OPENLAKEFORGE_CATALOG_FLOE_CLIENT_SECRET_KEY",
    "OPENLAKEFORGE_CATALOG_FLOE_CREDENTIALS_SECRET_NAME",
    "OPENLAKEFORGE_CATALOG_GLUE_REGION",
    "OPENLAKEFORGE_CATALOG_GLUE_WAREHOUSE_PREFIX",
    "OPENLAKEFORGE_CATALOG_NAME",
    "OPENLAKEFORGE_CATALOG_OAUTH_SCOPE",
    "OPENLAKEFORGE_CATALOG_PROVIDER",
    "OPENLAKEFORGE_CATALOG_REST_URI",
    "OPENLAKEFORGE_CATALOG_SCHEMA_PREFIX",
    "OPENLAKEFORGE_CATALOG_TOKEN_URI",
    "OPENLAKEFORGE_CATALOG_TYPE",
    "OPENLAKEFORGE_CATALOG_WAREHOUSE",
    "OPENLAKEFORGE_CATALOG_WAREHOUSE_PREFIX",
    "OPENLAKEFORGE_DBT_TRINO_USER",
    "OPENLAKEFORGE_GOVERNANCE_ENABLED",
    "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_JWT_KEY",
    "OPENLAKEFORGE_GOVERNANCE_INGESTION_BOT_SECRET_NAME",
    "OPENLAKEFORGE_QUERY_TRINO_HOST",
    "OPENLAKEFORGE_QUERY_TRINO_PORT",
    "OPENLAKEFORGE_QUERY_TRINO_TLS_PORT",
    "OPENLAKEFORGE_RUN_ARTIFACT_BASE_URI",
    "OPENLAKEFORGE_STORAGE_ACCESS_KEY_ID_KEY",
    "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET",
    "OPENLAKEFORGE_STORAGE_CREDENTIALS_SECRET_NAME",
    "OPENLAKEFORGE_STORAGE_ENDPOINT",
    "OPENLAKEFORGE_STORAGE_PATH_STYLE_ACCESS",
    "OPENLAKEFORGE_STORAGE_REGION",
    "OPENLAKEFORGE_STORAGE_SECRET_ACCESS_KEY_KEY",
    "OPENLAKEFORGE_STORAGE_SILVER_BUCKET",
)


class RuntimeContractError(RuntimeError):
    """The catalog's runtime contract declaration is missing or malformed."""


def binding_surface() -> dict[str, Any]:
    return {
        "env": sorted(RUNTIME_ENV),
        "aliases": sorted(RUNTIME_ALIASES),
        "query_ca_dir": QUERY_CA_DIR,
    }


def binding_fingerprint() -> str:
    rendered = json.dumps(binding_surface(), sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(rendered).hexdigest()


@dataclass(frozen=True)
class RuntimeContract:
    version: int
    accepts: tuple[int, ...]
    fingerprint: str | None = None

    def accepts_version(self, version: int) -> bool:
        return version in self.accepts


def parse_runtime_contract(catalog: dict[str, Any]) -> RuntimeContract:
    raw = (catalog.get("distribution") or {}).get("runtime_contract")
    if not isinstance(raw, dict):
        raise RuntimeContractError("component catalog has no distribution.runtime_contract")
    version = raw.get("version")
    accepts = raw.get("accepts")
    fingerprint = raw.get("binding_fingerprint")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RuntimeContractError(f"runtime_contract.version must be a positive integer, got {version!r}")
    if (
        not isinstance(accepts, list)
        or not accepts
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in accepts)
    ):
        raise RuntimeContractError("runtime_contract.accepts must be a non-empty list of integers")
    if version not in accepts:
        raise RuntimeContractError(f"runtime_contract.accepts {accepts} must include the current version {version}")
    if not isinstance(fingerprint, str):
        raise RuntimeContractError("runtime_contract.binding_fingerprint must be a string")
    return RuntimeContract(version=version, accepts=tuple(sorted(accepts)), fingerprint=fingerprint)


def runtime_contract_at(root: Path) -> RuntimeContract | None:
    """The contract a resolved distribution root ships, or None when it ships no catalog."""
    catalog_path = root / "release" / "component-catalog.yaml"
    if not catalog_path.is_file():
        return None
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict):
        raise RuntimeContractError(f"{catalog_path} did not parse to a mapping")
    return parse_runtime_contract(catalog)
