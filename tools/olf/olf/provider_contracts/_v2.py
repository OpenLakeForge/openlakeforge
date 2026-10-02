"""Reader for pre-v0.3 (schema 2.0.0) provider-contract state.

Isolated so removing it once no supported deployment can emit v2 (#114) is a
file deletion plus the dispatch branch in `parse_provider_contracts`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from olf.profile import StageName
from olf.provider_contracts._model import ProviderContracts, SharedPlatformContract, StageContract
from olf.provider_contracts._validation import V3_SCHEMA_VERSION, _frozen, _mapping


def _adapt_v2(payload: Mapping[str, Any]) -> ProviderContracts:
    """Lift the legacy flat contract to its one enabled DEV-stage equivalent."""
    storage = _mapping(payload.get("storage", {}), where="provider_contracts.storage")
    catalog = _mapping(payload.get("catalog", {}), where="provider_contracts.catalog")
    query = _mapping(payload.get("query", {}), where="provider_contracts.query")
    artifacts = _mapping(
        payload.get("artifact_bucket", payload.get("artifacts", {})), where="provider_contracts.artifact_bucket"
    )
    platform = _mapping(
        payload.get("kubernetes_platform", payload.get("cluster", {})), where="provider_contracts.kubernetes_platform"
    )
    provider = storage.get("provider", "local")
    shared_values = {
        "foundation": {"ref": "shared/foundation", "implementation": "legacy"},
        "kubernetes_platform": {"ref": "shared/kubernetes_platform", "implementation": "legacy"},
        "metadata_database": {"ref": "shared/metadata_database", "implementation": "legacy"},
        "query": {"ref": "shared/query", "implementation": "legacy"},
        "artifact_registry": {"ref": "shared/artifact_registry", "implementation": "legacy"},
        "ops_storage": {
            "ref": "shared/ops_storage",
            "implementation": "legacy",
            "bucket_name": artifacts.get("bucket_name", artifacts.get("ops_bucket_name", "openlakeforge-ops")),
            "artifact_base_uri": artifacts.get("artifact_base_uri", "s3://openlakeforge-ops"),
            "access_mode": artifacts.get("access_mode", "remote"),
            "local_upload_access_mode": artifacts.get("local_upload_access_mode", "direct"),
        },
        "secrets": {"ref": "shared/secrets", "implementation": "legacy"},
        "identity": {"ref": "shared/identity", "implementation": "legacy"},
        "access": {"ref": "shared/access", "implementation": "legacy"},
        "observability": {"ref": "shared/observability", "implementation": "legacy"},
    }
    shared = SharedPlatformContract(values=_frozen(shared_values))
    stage = StageContract(
        name=StageName.DEV,
        namespace=str(platform.get("namespace", "lakehouse")),
        storage=_frozen(
            {
                **storage,
                "identity_ref": "stage/dev/runtime_identity",
                "bronze": {
                    "physical_id": storage.get("bronze_bucket_name", "lakehouse-bronze"),
                    "bucket_name": storage.get("bronze_bucket_name", "lakehouse-bronze"),
                    "uri": "",
                },
                "silver": {
                    "physical_id": storage.get("silver_bucket_name", "lakehouse-silver"),
                    "bucket_name": storage.get("silver_bucket_name", "lakehouse-silver"),
                    "uri": "",
                },
                "gold": {
                    "physical_id": storage.get("gold_bucket_name", "lakehouse-gold"),
                    "bucket_name": storage.get("gold_bucket_name", "lakehouse-gold"),
                    "uri": "",
                },
            }
        ),
        catalog=_frozen(
            {**catalog, "physical_id": catalog.get("glue_catalog_id", catalog.get("catalog_name", "lakehouse_dev"))}
        ),
        query=_frozen(
            {
                "service_ref": "shared/query",
                "catalog_ref": "stage/dev/catalog",
                "catalog_name": query.get("catalog_name", "iceberg"),
                "endpoint": query.get("endpoint", "http://trino:8080"),
                "runtime_identity_ref": "stage/dev/runtime_identity",
            }
        ),
        orchestration=_frozen({"service_ref": "stage/dev/orchestration", "endpoint_ref": "legacy"}),
        activation=_frozen({"ops_storage_ref": "shared/ops_storage", "prefix": "activations/dev"}),
        endpoints=_frozen(
            {"catalog": "legacy-catalog", "query": "legacy-query", "orchestration": "legacy-orchestration"}
        ),
        runtime_identity=_frozen({"ref": "stage/dev/runtime_identity", "principal": f"legacy-{provider}-dev"}),
        reporting=None,
        governance=None,
        shared=shared,
    )
    return ProviderContracts(
        schema_version=V3_SCHEMA_VERSION,
        deployment=MappingProxyType({"profile_name": "legacy", "provider": provider, "region": storage.get("region")}),
        shared=shared,
        stages=MappingProxyType({StageName.DEV: stage}),
        compatibility_v2=True,
    )
