"""Stage-aware provider-contract parsing and compatibility adaptation.

Terraform remains the source of physical provider values. This module owns the
typed boundary that validates those values against a resolved deployment
topology before a runtime consumes them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from olf.deployment.context import Provider
from olf.profile import DeploymentTopology, StageName

V2_SCHEMA_VERSION = "2.0.0"
V3_SCHEMA_VERSION = "3.0.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({V2_SCHEMA_VERSION, V3_SCHEMA_VERSION})


class ProviderContractError(ValueError):
    """Raised when a provider contract is malformed or cannot serve a stage."""


def aws_catalog_name(profile_name: str, stage: StageName | str) -> str:
    """Return the provider-derived, Glue-safe catalog name for one stage."""
    stage_value = StageName(stage).value
    candidate = f"olf_{profile_name.replace('-', '_')}_{stage_value}".lower()
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
    suffix = f"_{stage_value}_{digest}"
    return f"{candidate[: 64 - len(suffix)]}{suffix}"


def _mapping(value: object, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderContractError(f"{where} must be an object")
    return value


def _string(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderContractError(f"{where} must be a non-empty string")
    return value


def _fields(
    value: object,
    *,
    where: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    document = _mapping(value, where=where)
    missing = required - set(document)
    unexpected = set(document) - required - optional
    if missing:
        raise ProviderContractError(f"{where} is missing required fields {sorted(missing)!r}")
    if unexpected:
        raise ProviderContractError(f"{where} contains unsupported fields {sorted(unexpected)!r}")
    return document


def _frozen(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def _tcp_port(value: object, *, where: str) -> None:
    """Reject a port that would blow up int() downstream instead of at the
    contract boundary (artifact_store.artifact_storage_client() and the
    port-forward path in commands/artifacts.py both call int() on the
    exported value without their own validation)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ProviderContractError(f"{where} must be a valid TCP port")
    text = str(value)
    if not text.isdigit() or not (1 <= int(text) <= 65535):
        raise ProviderContractError(f"{where} must be a valid TCP port")


def _stage_name(value: StageName | str) -> StageName:
    try:
        return StageName(value)
    except ValueError as exc:
        raise ProviderContractError(f"unknown stage {value!r}") from exc


def _reference(value: object, *, where: str, allowed: tuple[str, ...]) -> str:
    reference = _string(value, where=where)
    if not reference.startswith(allowed):
        allowed_text = ", ".join(allowed)
        raise ProviderContractError(f"{where} must reference one of {allowed_text}")
    return reference


def _http_host_port_uri(value: object, *, where: str) -> str:
    """A URI olf.contracts._apply_provider_contracts can extract host:port from.

    That adapter only recognizes the ``http://`` scheme (see its
    ``endpoint.startswith("http://")`` gate) and requires a colon-delimited
    port; anything else silently keeps the local Trino host/port defaults
    instead of routing to the declared service. Validate the same shape here
    so a scheme or port the adapter cannot use fails closed at the contract
    boundary instead of downstream.
    """
    uri = _string(value, where=where)
    if not uri.startswith("http://"):
        raise ProviderContractError(f"{where} must be an http:// URI")
    host_port = uri.removeprefix("http://").split("/", 1)[0]
    host, _, port = host_port.partition(":")
    if not host or not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ProviderContractError(f"{where} must be http://<host>:<port> with a valid TCP port")
    return uri


def _s3_uri_bucket(value: object, *, where: str) -> str:
    """Parse an s3://<bucket>[/prefix] URI and return its bucket component."""
    uri = _string(value, where=where)
    parts = urlsplit(uri)
    if parts.scheme != "s3" or not parts.netloc:
        raise ProviderContractError(f"{where} must be an s3://<bucket>[/prefix] URI")
    return parts.netloc


_GLUE_CATALOG_ID_PATTERN = re.compile(r"^\d{12}(:[a-z0-9_]+)?$")


def _check_glue_catalog_id(value: object, *, where: str) -> None:
    """A Glue CatalogId is either a bare 12-digit account ID (the shared
    default catalog - this account's Glue service refuses to create any
    other kind) or, for a provider whose account can create one, a
    '<account-id>:<name>' custom catalog. A suffix-only check (rsplit on
    ":") would accept a bare catalog *name* with no account-id at all -
    GlueClient then receives that as an unusable CatalogId and only fails at
    the AWS API instead of here."""
    catalog_id = _string(value, where=where)
    if not _GLUE_CATALOG_ID_PATTERN.match(catalog_id):
        raise ProviderContractError(f"{where} must be '<12-digit-account-id>[:<catalog-name>]'")


def _absolute_http_uri(value: object, *, where: str) -> str:
    """A URI with a real scheme and authority, fit to compare by origin.

    Floe's Iceberg REST client accepts either http or https, unlike the
    Trino query endpoint (_http_host_port_uri), so both schemes are valid
    here. Without this, two relative strings (urlsplit gives an empty
    scheme/netloc for e.g. "polaris") would compare as trivially
    "same origin" in _same_origin, and a non-string value would raise
    AttributeError/TypeError inside urlsplit's caller instead of
    ProviderContractError.
    """
    uri = _string(value, where=where)
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProviderContractError(f"{where} must be an absolute http:// or https:// URI")
    if parts.username is not None or parts.password is not None:
        # AGENTS.md: credentials never appear in the contract, only Secret
        # references - a URI with embedded userinfo would otherwise be
        # exported by build_contract_env() and written into Floe's
        # generated EnvironmentProfile.
        raise ProviderContractError(f"{where} must not embed credentials in its authority")
    try:
        port = parts.port
    except ValueError as exc:
        raise ProviderContractError(f"{where} must have a valid TCP port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ProviderContractError(f"{where} must have a valid TCP port")
    return uri


def _same_origin(left: str, right: str) -> bool:
    """Compare (scheme, host, port) of two already-validated absolute URIs."""
    a, b = urlsplit(left), urlsplit(right)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


_CATALOG_TYPES = frozenset({"rest", "glue"})
_CATALOG_PROVIDERS = frozenset({"polaris", "aws-glue"})
_CATALOG_TYPE_BY_PROVIDER = {"polaris": "rest", "aws-glue": "glue"}
_CATALOG_PROVIDER_BY_TOPOLOGY_PROVIDER = {
    Provider.LOCAL: "polaris",
    Provider.AZURE: "polaris",
    Provider.AWS: "aws-glue",
}
_STORAGE_IMPLEMENTATION_BY_TOPOLOGY_PROVIDER = {
    Provider.LOCAL: "storage.s3_compatible.seaweedfs",
    Provider.AZURE: "storage.s3_compatible.seaweedfs_on_aks",
    Provider.AWS: "storage.aws_s3",
}


# A Dagster code-location name becomes a Kubernetes Deployment and Service
# name. Services are RFC 1035, not DNS-1123: they must start with a letter
# unless the alpha RelaxedServiceNameValidation gate is on, which no root
# here enables. Validating the looser rule would accept a name like "1sales"
# that this parser calls fine and the API server then rejects mid-rollout.
# Matched with `fullmatch`, not `match`: `$` also matches just before a
# trailing newline, so "sales\n" would pass and reach the API server with the
# newline still in it.
_SERVICE_NAME_PATTERN = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
_PYTHON_MODULE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
# What a stage runs when its contract predates the field. A platform applied
# before #185 has a persisted 3.0.0 orchestration object with only the two
# refs, and its Dagster release is running the Terraform module's own default
# (modules/orchestration/dagster/variables.tf). Requiring the field instead
# would make every artifacts-phase deploy against that state demand a platform
# apply first, which is the lifecycle boundary ADR 0002 exists to hold.
_LEGACY_CODE_LOCATIONS = (
    MappingProxyType({"name": "openlakeforge-dagster", "definitions_module": "lakehouse_code.definitions"}),
)


def _code_locations(value: object, *, where: str) -> None:
    """Validate the stage's Dagster user-code deployments.

    `dagster-user-deployments` names each Service after its deployment, so a
    location name the API server would reject is not a cosmetic problem: the
    webserver workspace Terraform renders from this same list would then point
    at a host nothing ever creates, and the stage would come up with no code
    server reachable and no error at apply time.
    """
    if not isinstance(value, list) or not value:
        raise ProviderContractError(f"{where} must be a non-empty list")
    names: set[str] = set()
    for index, entry in enumerate(value):
        document = _fields(entry, where=f"{where}[{index}]", required={"name", "definitions_module"})
        name = _string(document["name"], where=f"{where}[{index}].name")
        if not _SERVICE_NAME_PATTERN.fullmatch(name):
            raise ProviderContractError(
                f"{where}[{index}].name must be an RFC 1035 label: lowercase alphanumerics or '-', "
                f"starting with a letter, as a Kubernetes Service name requires"
            )
        if name in names:
            raise ProviderContractError(f"{where} declares the code location {name!r} twice")
        names.add(name)
        module = _string(document["definitions_module"], where=f"{where}[{index}].definitions_module")
        if not _PYTHON_MODULE_PATTERN.fullmatch(module):
            raise ProviderContractError(f"{where}[{index}].definitions_module must be a dotted Python module path")


def _canonical_stage_reference(value: object, *, where: str, stage: StageName, path: str) -> str:
    """A stage-service binding pinned to its one canonical name.

    ADR 0011 classifies orchestration and reporting as per-stage services
    (never shared, unlike catalog/query/governance) - a prefix check such as
    ``stage/<name>/*`` would accept another same-stage binding entirely (e.g.
    orchestration.service_ref: stage/dev/catalog), so every stage-service
    service_ref/endpoint_ref must equal its own fixed path.
    """
    expected = f"stage/{stage.value}/{path}"
    reference = _string(value, where=where)
    if reference != expected:
        raise ProviderContractError(f"{where} must be {expected!r}")
    return reference


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
            # Absent means no lineage: only the stage the root names emits.
            "governance": {
                "enabled": self.governance is not None,
                "lineage": self.governance is not None and self.governance.get("lineage") is True,
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
    if _s3_uri_bucket(ops_storage["artifact_base_uri"], where="shared.ops_storage.artifact_base_uri") != ops_storage[
        "bucket_name"
    ]:
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
    # backward-compat rationale as _LEGACY_CODE_LOCATIONS above) has none.
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
            optional={"lineage"},
        )
        if not isinstance(governance.get("lineage", False), bool):
            raise ProviderContractError(f"stages.{name.value}.governance.lineage must be a boolean")
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
            f"deployment.region {deployment['region']!r} does not match DeploymentTopology.region "
            f"{topology.region!r}"
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
    # OpenMetadata's OpenLineage settings are global: one default pipeline
    # service and one catalog mapping. Two emitting stages would land in the
    # same pipeline service and resolve to one stage's entities (#131).
    lineage_stages = sorted(
        name.value for name, stage in stages.items() if stage.governance and stage.governance.get("lineage")
    )
    if len(lineage_stages) > 1:
        raise ProviderContractError(f"stages {lineage_stages!r} all emit lineage; at most one governed stage may")
    return ProviderContracts(
        schema_version=V3_SCHEMA_VERSION,
        deployment=_frozen(deployment),
        shared=shared,
        stages=MappingProxyType(stages),
    )


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


def parse_provider_contracts(
    payload: Mapping[str, Any], topology: DeploymentTopology | None = None
) -> ProviderContracts:
    """Parse v2/v3 provider output, rejecting unknown versions and shapes."""
    schema_version = payload.get("schema_version")
    if schema_version == V2_SCHEMA_VERSION:
        return _adapt_v2(payload)
    if schema_version == V3_SCHEMA_VERSION:
        return _parse_v3(payload, topology)
    raise ProviderContractError(
        f"provider_contracts.schema_version {schema_version!r} is unsupported; "
        f"expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}"
    )
