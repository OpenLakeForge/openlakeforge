"""Native provider-contract v3 reader."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from olf.deployment.context import Provider
from olf.profile import DeploymentTopology, StageName
from olf.provider_contracts._model import ProviderContracts, SharedPlatformContract, StageContract
from olf.provider_contracts._validation import (
    _CATALOG_PROVIDER_BY_TOPOLOGY_PROVIDER,
    _CATALOG_PROVIDERS,
    _CATALOG_TYPE_BY_PROVIDER,
    _CATALOG_TYPES,
    _STORAGE_IMPLEMENTATION_BY_TOPOLOGY_PROVIDER,
    V3_SCHEMA_VERSION,
    ProviderContractError,
    _absolute_http_uri,
    _canonical_stage_reference,
    _check_glue_catalog_id,
    _code_locations,
    _fields,
    _frozen,
    _http_host_port_uri,
    _mapping,
    _reference,
    _s3_uri_bucket,
    _same_origin,
    _stage_name,
    _string,
    _tcp_port,
)


def _parse_shared(value: object) -> SharedPlatformContract:
    required = {
        "foundation",
        "kubernetes_platform",
        "metadata_database",
        "query",
        "artifact_registry",
        "ops_storage",
        "secrets",
        "identity",
        "access",
        "observability",
    }
    optional = {"catalog_service", "governance_service"}
    document = _fields(value, where="shared", required=required, optional=optional)
    parsed: dict[str, Mapping[str, Any]] = {}
    for name, binding in document.items():
        parsed[name] = _fields(
            binding,
            where=f"shared.{name}",
            required={"ref", "implementation"},
            optional={
                "endpoint",
                "bucket_name",
                "artifact_base_uri",
                "access_mode",
                "local_upload_access_mode",
                # Only meaningful on shared.ops_storage: the shared admin
                # identity olf's own revision-publish/manifest-upload
                # tooling uses. Every stage's own OPENLAKEFORGE_STORAGE_*
                # (parsed separately, per stage) is deliberately narrower -
                # see StageContract.as_v2_environment_contract.
                "credentials_secret_name",
                "access_key_id_key",
                "secret_access_key_key",
            },
        )
        _reference(parsed[name]["ref"], where=f"shared.{name}.ref", allowed=("shared/",))
        if parsed[name]["ref"] != f"shared/{name}":
            raise ProviderContractError(f"shared.{name}.ref must be 'shared/{name}'")
    ops_storage = parsed["ops_storage"]
    for field in ("bucket_name", "artifact_base_uri"):
        _string(ops_storage.get(field), where=f"shared.ops_storage.{field}")
    if (
        _s3_uri_bucket(ops_storage["artifact_base_uri"], where="shared.ops_storage.artifact_base_uri")
        != ops_storage["bucket_name"]
    ):
        raise ProviderContractError("shared.ops_storage.artifact_base_uri must address its own bucket_name")
    return SharedPlatformContract(values=_frozen(parsed))


def _parse_stage(
    name: StageName,
    value: object,
    *,
    shared: SharedPlatformContract,
    topology: DeploymentTopology,
) -> StageContract:
    shared_refs = {binding["ref"] for binding in shared.values.values()}
    document = _fields(
        value,
        where=f"stages.{name.value}",
        required={
            "namespace",
            "storage",
            "catalog",
            "query",
            "orchestration",
            "activation",
            "endpoints",
            "runtime_identity",
        },
        optional={"reporting", "governance"},
    )
    namespace = _string(document["namespace"], where=f"stages.{name.value}.namespace")
    storage = _fields(
        document["storage"],
        where=f"stages.{name.value}.storage",
        required={"provider", "implementation", "protocol", "region", "identity_ref", "bronze", "silver", "gold"},
        optional={
            "endpoint",
            "virtual_host_endpoint",
            "path_style_access",
            "ssl_mode",
            "credentials_secret_name",
            "access_key_id_key",
            "secret_access_key_key",
            "s3_service_name",
            "s3_service_namespace",
            "s3_service_port",
        },
    )
    if storage["provider"] != topology.provider.value:
        raise ProviderContractError(f"stages.{name.value}.storage.provider must match DeploymentTopology.provider")
    if storage["implementation"] != _STORAGE_IMPLEMENTATION_BY_TOPOLOGY_PROVIDER[topology.provider]:
        raise ProviderContractError(
            f"stages.{name.value}.storage.implementation must match DeploymentTopology.provider"
        )
    _string(storage["region"], where=f"stages.{name.value}.storage.region")
    if topology.region is not None and storage["region"] != topology.region:
        raise ProviderContractError(f"stages.{name.value}.storage.region must match DeploymentTopology.region")
    if "s3_service_port" in storage:
        _tcp_port(storage["s3_service_port"], where=f"stages.{name.value}.storage.s3_service_port")
    if "endpoint" in storage:
        _absolute_http_uri(storage["endpoint"], where=f"stages.{name.value}.storage.endpoint")
    _reference(storage["identity_ref"], where=f"stages.{name.value}.storage.identity_ref", allowed=("stage/",))
    physical_storage: set[str] = set()
    for layer in ("bronze", "silver", "gold"):
        binding = _fields(
            storage[layer],
            where=f"stages.{name.value}.storage.{layer}",
            required={"physical_id", "bucket_name", "uri"},
        )
        physical_id = _string(binding["physical_id"], where=f"stages.{name.value}.storage.{layer}.physical_id")
        if physical_id in physical_storage:
            raise ProviderContractError(f"stages.{name.value}.storage reuses physical identity {physical_id!r}")
        physical_storage.add(physical_id)
        _string(binding["bucket_name"], where=f"stages.{name.value}.storage.{layer}.bucket_name")
        uri_bucket = _s3_uri_bucket(binding["uri"], where=f"stages.{name.value}.storage.{layer}.uri")
        if uri_bucket != binding["bucket_name"]:
            raise ProviderContractError(f"stages.{name.value}.storage.{layer}.uri must address its own bucket_name")
    catalog = _fields(
        document["catalog"],
        where=f"stages.{name.value}.catalog",
        required={
            "logical_name",
            "implementation",
            "catalog_type",
            "catalog_provider",
            "catalog_name",
            "runtime_profile",
            "physical_id",
        },
        optional={
            "service_ref",
            "warehouse",
            "rest_uri",
            "token_uri",
            "oauth_scope",
            "glue_region",
            "glue_catalog_id",
            "glue_rest_uri",
            "glue_rest_warehouse",
            "glue_warehouse_prefix",
            "catalog_namespace_model",
            "floe_credentials_secret_name",
            "floe_client_id_key",
            "floe_client_secret_key",
            "deployer_credentials_secret_name",
            "deployer_client_id_key",
            "deployer_client_secret_key",
        },
    )
    _string(catalog["physical_id"], where=f"stages.{name.value}.catalog.physical_id")
    catalog_type = catalog["catalog_type"]
    catalog_provider = catalog["catalog_provider"]
    if catalog_type not in _CATALOG_TYPES:
        raise ProviderContractError(
            f"stages.{name.value}.catalog.catalog_type must be one of {sorted(_CATALOG_TYPES)!r}"
        )
    if catalog_provider not in _CATALOG_PROVIDERS:
        raise ProviderContractError(
            f"stages.{name.value}.catalog.catalog_provider must be one of {sorted(_CATALOG_PROVIDERS)!r}"
        )
    if _CATALOG_TYPE_BY_PROVIDER[catalog_provider] != catalog_type:
        raise ProviderContractError(
            f"stages.{name.value}.catalog.catalog_type {catalog_type!r} does not match "
            f"catalog_provider {catalog_provider!r}"
        )
    if catalog_provider != _CATALOG_PROVIDER_BY_TOPOLOGY_PROVIDER[topology.provider]:
        raise ProviderContractError(
            f"stages.{name.value}.catalog.catalog_provider {catalog_provider!r} does not match "
            f"DeploymentTopology.provider {topology.provider.value!r}"
        )
    expected_catalog = f"lakehouse_{name.value}"
    if catalog["catalog_name"] != expected_catalog:
        raise ProviderContractError(f"stages.{name.value}.catalog.catalog_name must be canonical {expected_catalog!r}")
    if catalog["catalog_provider"] == "aws-glue":
        _string(catalog.get("glue_region"), where=f"stages.{name.value}.catalog.glue_region")
        _check_glue_catalog_id(catalog.get("glue_catalog_id"), where=f"stages.{name.value}.catalog.glue_catalog_id")
        if topology.region is not None and catalog["glue_region"] != topology.region:
            raise ProviderContractError(f"stages.{name.value}.catalog.glue_region must match DeploymentTopology.region")
        if catalog["glue_catalog_id"] != catalog["physical_id"]:
            raise ProviderContractError(f"stages.{name.value}.catalog Glue catalog ID must be its physical identity")
        if "glue_rest_warehouse" in catalog and catalog["glue_rest_warehouse"] != catalog["glue_catalog_id"]:
            raise ProviderContractError(
                f"stages.{name.value}.catalog.glue_rest_warehouse must match its own Glue catalog ID"
            )
        # No per-stage Glue catalog name to validate here: this account's
        # Glue service refuses to create one (confirmed against the AWS API,
        # not just this module's config), so every stage's glue_catalog_id
        # is the account's one shared default catalog - the canonical-name
        # check just above (catalog_name == "lakehouse_<stage>") is what
        # keeps each stage's *database* names collision-free within it.
    elif "service_ref" not in catalog:
        raise ProviderContractError(f"stages.{name.value}.catalog.service_ref is required for a Polaris catalog")
    if "service_ref" in catalog:
        _reference(catalog["service_ref"], where=f"stages.{name.value}.catalog.service_ref", allowed=("shared/",))
        if catalog["service_ref"] not in shared_refs:
            raise ProviderContractError(f"stages.{name.value}.catalog.service_ref does not resolve")
        catalog_service_ref = shared.values.get("catalog_service", {}).get("ref")
        if catalog["service_ref"] != catalog_service_ref:
            raise ProviderContractError(
                f"stages.{name.value}.catalog.service_ref must reference the shared catalog service"
            )
        rest_uri = catalog.get("rest_uri")
        if rest_uri is not None:
            rest_uri = _absolute_http_uri(rest_uri, where=f"stages.{name.value}.catalog.rest_uri")
        catalog_service_endpoint = shared.values.get("catalog_service", {}).get("endpoint")
        if catalog_service_endpoint is not None:
            catalog_service_endpoint = _absolute_http_uri(
                catalog_service_endpoint, where="shared.catalog_service.endpoint"
            )
            if rest_uri != catalog_service_endpoint:
                raise ProviderContractError(
                    f"stages.{name.value}.catalog.rest_uri must match the shared catalog service's endpoint"
                )
        if rest_uri is not None and "token_uri" not in catalog:
            raise ProviderContractError(f"stages.{name.value}.catalog.token_uri is required when rest_uri is supplied")
        if "token_uri" in catalog:
            if rest_uri is None:
                raise ProviderContractError(f"stages.{name.value}.catalog.token_uri requires rest_uri")
            token_uri = _absolute_http_uri(catalog["token_uri"], where=f"stages.{name.value}.catalog.token_uri")
            if not _same_origin(token_uri, rest_uri):
                raise ProviderContractError(
                    f"stages.{name.value}.catalog.token_uri must share its own rest_uri's scheme and host:port"
                )
    if "warehouse" in catalog and catalog["warehouse"] != catalog["physical_id"]:
        raise ProviderContractError(f"stages.{name.value}.catalog.warehouse must match its own physical identity")
    query = _fields(
        document["query"],
        where=f"stages.{name.value}.query",
        required={"service_ref", "catalog_ref", "catalog_name", "endpoint", "runtime_identity_ref"},
    )
    _http_host_port_uri(query["endpoint"], where=f"stages.{name.value}.query.endpoint")
    _reference(query["service_ref"], where=f"stages.{name.value}.query.service_ref", allowed=("shared/",))
    if query["service_ref"] not in shared_refs:
        raise ProviderContractError(f"stages.{name.value}.query.service_ref does not resolve")
    if query["service_ref"] != shared.values["query"]["ref"]:
        raise ProviderContractError(f"stages.{name.value}.query.service_ref must reference the shared query service")
    if query["catalog_ref"] != f"stage/{name.value}/catalog":
        raise ProviderContractError(f"stages.{name.value}.query.catalog_ref cannot reference another stage")
    if query["catalog_name"] != expected_catalog:
        raise ProviderContractError(f"stages.{name.value}.query.catalog_name must be {expected_catalog!r}")
    orchestration = _fields(
        document["orchestration"],
        where=f"stages.{name.value}.orchestration",
        required={"service_ref", "endpoint_ref"},
        optional={"code_locations", "pipeline_service_name"},
    )
    if "code_locations" in orchestration:
        _code_locations(orchestration["code_locations"], where=f"stages.{name.value}.orchestration.code_locations")
    _canonical_stage_reference(
        orchestration["service_ref"],
        where=f"stages.{name.value}.orchestration.service_ref",
        stage=name,
        path="orchestration",
    )
    _canonical_stage_reference(
        orchestration["endpoint_ref"],
        where=f"stages.{name.value}.orchestration.endpoint_ref",
        stage=name,
        path="endpoints/orchestration",
    )
    # Every enabled stage runs its own Dagster instance regardless of
    # governance, so this deterministic root name lives on orchestration,
    # not gated behind the governance block below. #131: a future OpenMetadata
    # bootstrap loops over the governed subset of `stages` and needs each
    # one's pipeline-service root without re-deriving it from the stage name.
    # Optional because a persisted v3 contract from before this field (same
    # backward-compat rationale as _model._LEGACY_CODE_LOCATIONS) has none.
    if "pipeline_service_name" in orchestration:
        expected_pipeline_service = f"dagster_{name.value}"
        if orchestration["pipeline_service_name"] != expected_pipeline_service:
            raise ProviderContractError(
                f"stages.{name.value}.orchestration.pipeline_service_name must be canonical "
                f"{expected_pipeline_service!r}"
            )
    activation = _fields(
        document["activation"],
        where=f"stages.{name.value}.activation",
        required={"ops_storage_ref", "prefix"},
    )
    if activation["ops_storage_ref"] != "shared/ops_storage":
        raise ProviderContractError(f"stages.{name.value}.activation must use shared ops storage")
    if activation["prefix"] != f"activations/{name.value}":
        raise ProviderContractError(f"stages.{name.value}.activation.prefix must be activations/{name.value!s}")
    endpoints = _fields(
        document["endpoints"],
        where=f"stages.{name.value}.endpoints",
        required={"catalog", "query", "orchestration"},
        optional={"reporting", "governance"},
    )
    if endpoints["query"] != query["service_ref"]:
        raise ProviderContractError(f"stages.{name.value}.endpoints.query must resolve the query service")
    if endpoints["orchestration"] != orchestration["endpoint_ref"]:
        raise ProviderContractError(f"stages.{name.value}.endpoints.orchestration must resolve orchestration")
    expected_catalog_endpoint = catalog["service_ref"] if "service_ref" in catalog else f"stage/{name.value}/catalog"
    if endpoints["catalog"] != expected_catalog_endpoint:
        raise ProviderContractError(f"stages.{name.value}.endpoints.catalog must resolve the stage catalog")
    runtime_identity = _fields(
        document["runtime_identity"],
        where=f"stages.{name.value}.runtime_identity",
        required={"ref", "principal"},
    )
    identity_ref = f"stage/{name.value}/runtime_identity"
    if runtime_identity["ref"] != identity_ref:
        raise ProviderContractError(f"stages.{name.value}.runtime_identity.ref must be {identity_ref!r}")
    _string(runtime_identity["principal"], where=f"stages.{name.value}.runtime_identity.principal")
    if storage["identity_ref"] != identity_ref or query["runtime_identity_ref"] != identity_ref:
        raise ProviderContractError(f"stages.{name.value} runtime bindings must use their own runtime identity")

    resolved_stage = topology.stage(name)
    if resolved_stage is None:
        raise ProviderContractError(f"DeploymentTopology has no {name.value!r} stage")
    reporting = document.get("reporting")
    governance = document.get("governance")
    if resolved_stage.capabilities.analytics != (reporting is not None):
        raise ProviderContractError(f"stages.{name.value}.reporting must match the analytics capability")
    if resolved_stage.capabilities.governance != (governance is not None):
        raise ProviderContractError(f"stages.{name.value}.governance must match the governance capability")
    if reporting is not None:
        reporting = _fields(
            reporting,
            where=f"stages.{name.value}.reporting",
            required={"service_ref", "endpoint_ref"},
            optional={"dashboard_service_name"},
        )
        _canonical_stage_reference(
            reporting["service_ref"],
            where=f"stages.{name.value}.reporting.service_ref",
            stage=name,
            path="reporting",
        )
        _canonical_stage_reference(
            reporting["endpoint_ref"],
            where=f"stages.{name.value}.reporting.endpoint_ref",
            stage=name,
            path="endpoints/reporting",
        )
        # Deterministic OpenMetadata dashboard-service root (#131), same
        # backward-compat optionality as orchestration.pipeline_service_name
        # above - only present on an analytics stage, since that is the only
        # case Terraform has a Superset instance to name.
        if "dashboard_service_name" in reporting:
            expected_dashboard_service = f"superset_{name.value}"
            if reporting["dashboard_service_name"] != expected_dashboard_service:
                raise ProviderContractError(
                    f"stages.{name.value}.reporting.dashboard_service_name must be canonical "
                    f"{expected_dashboard_service!r}"
                )
        if endpoints.get("reporting") != reporting["endpoint_ref"]:
            raise ProviderContractError(f"stages.{name.value}.endpoints.reporting must resolve reporting")
    elif "reporting" in endpoints:
        raise ProviderContractError(f"stages.{name.value}.endpoints.reporting must be absent without reporting")
    if governance is not None:
        governance = _fields(
            governance,
            where=f"stages.{name.value}.governance",
            required={"service_ref", "endpoint_ref"},
        )
        if governance["service_ref"] not in shared_refs:
            raise ProviderContractError(f"stages.{name.value}.governance.service_ref does not resolve")
        governance_service_ref = shared.values.get("governance_service", {}).get("ref")
        if governance["service_ref"] != governance_service_ref:
            raise ProviderContractError(
                f"stages.{name.value}.governance.service_ref must reference the shared governance service"
            )
        _canonical_stage_reference(
            governance["endpoint_ref"],
            where=f"stages.{name.value}.governance.endpoint_ref",
            stage=name,
            path="endpoints/governance",
        )
        if endpoints.get("governance") != governance["endpoint_ref"]:
            raise ProviderContractError(f"stages.{name.value}.endpoints.governance must resolve governance")
    elif "governance" in endpoints:
        raise ProviderContractError(f"stages.{name.value}.endpoints.governance must be absent without governance")
    return StageContract(
        name=name,
        namespace=namespace,
        storage=_frozen(storage),
        catalog=_frozen(catalog),
        query=_frozen(query),
        orchestration=_frozen(orchestration),
        activation=_frozen(activation),
        endpoints=_frozen(endpoints),
        runtime_identity=_frozen(runtime_identity),
        reporting=_frozen(reporting) if reporting is not None else None,
        governance=_frozen(governance) if governance is not None else None,
        shared=shared,
    )


def _parse_v3(payload: Mapping[str, Any], topology: DeploymentTopology | None) -> ProviderContracts:
    if topology is None:
        raise ProviderContractError("native provider-contract v3 requires a resolved DeploymentTopology")
    document = _fields(
        payload, where="provider_contracts", required={"schema_version", "deployment", "shared", "stages"}
    )
    deployment = _fields(
        document["deployment"],
        where="deployment",
        required={"profile_name", "provider", "region"},
    )
    try:
        provider = Provider(_string(deployment["provider"], where="deployment.provider"))
    except ValueError as exc:
        raise ProviderContractError("deployment.provider is unsupported") from exc
    # Name both sides: the mismatch a caller actually hits is a command
    # resolving a different Deployment Profile than the deployment recorded,
    # and a message without the two values sends them to the CI log instead
    # of to the profile they need to name.
    if provider != topology.provider:
        raise ProviderContractError(
            f"deployment.provider {provider.value!r} does not match DeploymentTopology.provider "
            f"{topology.provider.value!r}"
        )
    if deployment["profile_name"] != topology.profile_name:
        raise ProviderContractError(
            f"deployment.profile_name {deployment['profile_name']!r} does not match "
            f"DeploymentTopology.profile_name {topology.profile_name!r}"
        )
    if deployment["region"] != topology.region:
        raise ProviderContractError(
            f"deployment.region {deployment['region']!r} does not match DeploymentTopology.region {topology.region!r}"
        )
    shared = _parse_shared(document["shared"])
    stages_document = _mapping(document["stages"], where="stages")
    expected_names = {stage.name.value for stage in topology.stages if stage.enabled}
    actual_names = set(stages_document)
    if actual_names != expected_names:
        raise ProviderContractError(
            f"contract stages {sorted(actual_names)!r} must equal enabled topology stages {sorted(expected_names)!r}"
        )
    stages: dict[StageName, StageContract] = {}
    physical_storage: set[str] = set()
    storage_bucket_names: set[str] = set()
    storage_uris: set[str] = set()
    catalog_ids: set[str] = set()
    principals: set[str] = set()
    stage_endpoint_values: set[str] = set()
    namespaces: set[str] = set()
    # Anchor to shared.query's own endpoint when the binding declares one (AWS's
    # fixture does not, so the first stage's endpoint is the anchor there) -
    # otherwise every stage could agree on a value that still diverges from the
    # one shared Trino service the binding actually names.
    shared_query_endpoint: str | None = shared.values["query"].get("endpoint")
    for raw_name, stage_value in stages_document.items():
        name = _stage_name(raw_name)
        stage = _parse_stage(name, stage_value, shared=shared, topology=topology)
        stages[name] = stage
        if stage.namespace in namespaces:
            raise ProviderContractError(f"namespace {stage.namespace!r} is shared between stages")
        namespaces.add(stage.namespace)
        query_endpoint = stage.query["endpoint"]
        if shared_query_endpoint is None:
            shared_query_endpoint = query_endpoint
        elif query_endpoint != shared_query_endpoint:
            raise ProviderContractError(f"stages.{name.value}.query.endpoint must match the shared query service")
        for layer in ("bronze", "silver", "gold"):
            layer_binding = stage.storage[layer]
            physical_id = layer_binding["physical_id"]
            if physical_id in physical_storage:
                raise ProviderContractError(f"storage physical identity {physical_id!r} is shared between stages")
            physical_storage.add(physical_id)
            bucket_name = layer_binding["bucket_name"]
            if bucket_name in storage_bucket_names:
                raise ProviderContractError(f"storage bucket name {bucket_name!r} is shared between stages")
            storage_bucket_names.add(bucket_name)
            uri = layer_binding["uri"]
            if uri and uri in storage_uris:
                raise ProviderContractError(f"storage location {uri!r} is shared between stages")
            if uri:
                storage_uris.add(uri)
        # A Glue-provider catalog's physical_id is the account's one shared
        # default catalog (this account's Glue service refuses to create a
        # custom catalog per stage) - every stage's is identical by design,
        # so uniqueness has to be checked against (physical_id, catalog_name)
        # instead: two stages genuinely collide only if they'd also share
        # their database-name prefix. A Polaris catalog's physical_id is
        # already unique per stage, so this pair is strictly stronger there
        # too, not a relaxation.
        catalog_identity = (stage.catalog["physical_id"], stage.catalog["catalog_name"])
        if catalog_identity in catalog_ids:
            raise ProviderContractError(f"catalog identity {catalog_identity!r} is shared between stages")
        catalog_ids.add(catalog_identity)
        principal = stage.runtime_identity["principal"]
        if principal in principals:
            raise ProviderContractError(f"runtime principal {principal!r} is shared between stages")
        principals.add(principal)
        for endpoint_name in ("orchestration", "reporting", "governance"):
            endpoint = stage.endpoints.get(endpoint_name)
            if endpoint is None:
                continue
            if endpoint in stage_endpoint_values:
                raise ProviderContractError(f"stage endpoint {endpoint!r} is shared between stages")
            stage_endpoint_values.add(endpoint)
    return ProviderContracts(
        schema_version=V3_SCHEMA_VERSION,
        deployment=_frozen(deployment),
        shared=shared,
        stages=MappingProxyType(stages),
    )
