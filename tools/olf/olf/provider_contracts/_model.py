"""Typed provider-contract values handed to runtime consumers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from olf.profile import StageName
from olf.provider_contracts._validation import V2_SCHEMA_VERSION, ProviderContractError, _mapping, _stage_name, _string

# What a stage runs when its contract predates the field. A platform applied
# before #185 has a persisted 3.0.0 orchestration object with only the two
# refs, and its Dagster release is running the Terraform module's own default
# (modules/orchestration/dagster/variables.tf). Requiring the field instead
# would make every artifacts-phase deploy against that state demand a platform
# apply first, which is the lifecycle boundary ADR 0002 exists to hold.
_LEGACY_CODE_LOCATIONS = (
    MappingProxyType({"name": "openlakeforge-dagster", "definitions_module": "lakehouse_code.definitions"}),
)


@dataclass(frozen=True)
class CodeLocation:
    """One Dagster user-code deployment: its in-cluster name and its module."""

    name: str
    definitions_module: str


@dataclass(frozen=True)
class SharedPlatformContract:
    """Provider-owned services that must not be repeated for every stage."""

    values: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class StageContract:
    """One selected stage's provider bindings and runtime environment shape."""

    name: StageName
    namespace: str
    storage: Mapping[str, Any]
    catalog: Mapping[str, Any]
    query: Mapping[str, Any]
    orchestration: Mapping[str, Any]
    activation: Mapping[str, Any]
    endpoints: Mapping[str, Any]
    runtime_identity: Mapping[str, Any]
    reporting: Mapping[str, Any] | None
    governance: Mapping[str, Any] | None
    shared: SharedPlatformContract

    @property
    def code_locations(self) -> tuple[CodeLocation, ...]:
        """The stage's Dagster code locations, in contract order.

        A contract that predates the field (v2, or a v3 document emitted before
        #185) resolves to the single merged location those platforms are
        actually running, so a code commit can still deploy against Terraform
        state nobody has re-applied.
        """
        return tuple(
            CodeLocation(name=entry["name"], definitions_module=entry["definitions_module"])
            for entry in self.orchestration.get("code_locations", _LEGACY_CODE_LOCATIONS)
        )

    def as_v2_environment_contract(self) -> dict[str, Any]:
        """Adapt a selected v3 stage to the existing runtime environment API."""
        layers = {
            layer: _mapping(self.storage[layer], where=f"storage.{layer}") for layer in ("bronze", "silver", "gold")
        }
        storage = {
            key: value for key, value in self.storage.items() if key not in {"bronze", "silver", "gold", "identity_ref"}
        }
        storage.update(
            {
                "bronze_bucket_name": layers["bronze"]["bucket_name"],
                "silver_bucket_name": layers["silver"]["bucket_name"],
                "gold_bucket_name": layers["gold"]["bucket_name"],
                "bucket_name": layers["bronze"]["bucket_name"],
            }
        )
        catalog = {key: value for key, value in self.catalog.items() if key not in {"physical_id", "service_ref"}}
        ops_storage = self.shared.values["ops_storage"]
        base_uri = _string(ops_storage["artifact_base_uri"], where="shared.ops_storage.artifact_base_uri").rstrip("/")
        prefix = _string(self.activation["prefix"], where=f"stages.{self.name}.activation.prefix").strip("/")
        stage_uri = f"{base_uri}/{prefix}"
        return {
            "schema_version": V2_SCHEMA_VERSION,
            "storage": storage,
            "catalog": catalog,
            "artifact_bucket": {
                "bucket_name": ops_storage["bucket_name"],
                "artifact_base_uri": stage_uri,
                "access_mode": ops_storage.get("access_mode", "remote"),
                "base_uri": f"{stage_uri}/floe/manifests",
                "floe_report_base_uri": f"{stage_uri}/floe/reports",
                "log_base_uri": f"{stage_uri}/logs",
                "run_artifact_base_uri": f"{stage_uri}/run-artifacts",
                "local_upload_access_mode": ops_storage.get("local_upload_access_mode", "direct"),
                # The shared admin identity, not this stage's own storage
                # credentials: immutable revision publishing
                # (olf.revision.REVISION_PREFIX = "floe/revisions") writes
                # outside any stage's activations/<stage> prefix, so only
                # the admin identity - scoped to the whole ops bucket - can
                # reach it.
                "credentials_secret_name": ops_storage.get("credentials_secret_name"),
                "access_key_id_key": ops_storage.get("access_key_id_key"),
                "secret_access_key_key": ops_storage.get("secret_access_key_key"),
            },
            "kubernetes_platform": {"namespace": self.namespace},
            "query": {
                "catalog_name": self.query["catalog_name"],
                "endpoint": self.query["endpoint"],
                "runtime_identity_principal": self.runtime_identity["principal"],
            },
            # Lineage is emitted under the stage's own pipeline-service root,
            # so OpenMetadata's auto-created pipelines stay distinct per stage.
            "governance": {
                "enabled": self.governance is not None,
                "lineage_namespace": self.orchestration.get("pipeline_service_name", f"dagster_{self.name.value}"),
            },
            "reporting": {"enabled": self.reporting is not None},
        }


@dataclass(frozen=True)
class ProviderContracts:
    """A provider contract that can expose only explicitly enabled stages."""

    schema_version: str
    deployment: Mapping[str, Any]
    shared: SharedPlatformContract
    stages: Mapping[StageName, StageContract]
    compatibility_v2: bool = False

    def for_stage(self, stage: StageName | str | None = None) -> StageContract:
        if stage is None:
            if self.compatibility_v2:
                return self.stages[StageName.DEV]
            raise ProviderContractError("native provider-contract v3 requires an explicit stage")
        stage_name = _stage_name(stage)
        try:
            return self.stages[stage_name]
        except KeyError as exc:
            raise ProviderContractError(f"provider contract has no enabled {stage_name.value!r} stage") from exc

    @property
    def governed_stages(self) -> Mapping[StageName, StageContract]:
        """Every enabled stage whose governance capability is on, keyed by
        stage name. #131: the shared OpenMetadata instance's single Iceberg/
        Dagster/Superset connection today comes from Terraform collapsing
        this same set to one stage (main.tf's governance_dagster_stage); this
        map is what a future bootstrap loops over for the database and
        pipeline roots (catalog.catalog_name,
        orchestration.pipeline_service_name).

        Not for dashboard roots -- see `analytics_stages`. Analytics and
        governance are independent per-stage capabilities (ADR 0011), so the
        two sets differ whenever a stage enables one and not the other."""
        return MappingProxyType({name: stage for name, stage in self.stages.items() if stage.governance is not None})

    @property
    def analytics_stages(self) -> Mapping[StageName, StageContract]:
        """Every enabled stage whose analytics capability is on, keyed by
        stage name. Separate from `governed_stages` because the capabilities
        are independent: with governance on DEV and analytics on PROD, looping
        the governed set alone would register DEV's services and silently skip
        `superset_prod`, which is the one Superset root that exists. Terraform
        already draws this distinction -- main.tf selects the Superset
        connection from `analytics_stages`, not the governed set."""
        return MappingProxyType({name: stage for name, stage in self.stages.items() if stage.reporting is not None})
