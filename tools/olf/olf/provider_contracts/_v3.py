"""Native provider-contract v3 reader."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import yaml

from olf import config
from olf.deployment.context import Provider
from olf.profile import _BASE_DOMAIN_PATTERN, DeploymentTopology, IdentitySpec, StageName
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
    _oidc_issuer_url,
    _reference,
    _s3_uri_bucket,
    _same_origin,
    _stage_name,
    _string,
    _tcp_port,
)

# The ingress shape of shared.access (ADR 0013). All four or none: a
# port-forward contract carries none of them.
_ACCESS_INGRESS_FIELDS = {"base_domain", "issuer", "tls_mode", "routes"}
# Services a route may publish to users, by the last segment of their contract
# ref. Everything else (metadata database, ops storage, catalog service,
# registry, ...) can only be routed as internal, and Dagster code servers have
# no ref at all, so no route can name them.
_USER_FACING_SERVICES = frozenset({"orchestration", "reporting", "governance_service", "query", "identity", "portal"})
# The landing page is the one route on the apex: https://<base_domain>.
_PORTAL_REF = "shared/portal"
_ROUTE_EXPOSURES = frozenset({"user-facing", "internal"})
# The ingress terminates TLS with a certificate from the issuer; the only mode
# the local Traefik/cert-manager adapter implements.
_TLS_MODES = frozenset({"ingress-terminated"})
# Shared bindings that are platform plumbing rather than network services, so
# an ingress has no backend to send a route to.
_NON_SERVICE_BINDINGS = frozenset({"foundation", "kubernetes_platform", "secrets", "access", "observability"})

# The role model's grants are keyed by route service name, so only services a
# route can publish to users can carry one; identity is the login surface and
# is never behind the perimeter, and the portal is the post-login landing page
# every authenticated user reaches, so it is never role-gated either.
_GRANTABLE_SERVICES = _USER_FACING_SERVICES - {"identity", "portal"}
IDENTITY_ROLES_PATH = "release/identity-roles.yaml"


def _identity_roles(value: object, *, where: str) -> Mapping[str, Any]:
    document = _fields(value, where=where, required={"precedence", "grants"})
    precedence = document["precedence"]
    if not isinstance(precedence, list) or not precedence:
        raise ProviderContractError(f"{where}.precedence must be a non-empty list of unique role names")
    for role in precedence:
        _string(role, where=f"{where}.precedence entry")
    if len(set(precedence)) != len(precedence):
        raise ProviderContractError(f"{where}.precedence must be a non-empty list of unique role names")
    for service, by_role in _mapping(document["grants"], where=f"{where}.grants").items():
        if service not in _GRANTABLE_SERVICES:
            raise ProviderContractError(f"{where}.grants names unknown service {service!r}")
        grants = _mapping(by_role, where=f"{where}.grants.{service}")
        for role, in_service_role in grants.items():
            if role not in precedence:
                raise ProviderContractError(f"{where}.grants.{service} names unknown role {role!r}")
            _string(in_service_role, where=f"{where}.grants.{service}.{role}")
        # The held roles must be a prefix of the precedence order, which is
        # what makes "union of held roles" the same as "highest held role".
        held = [role in grants for role in precedence]
        if held != sorted(held, reverse=True):
            raise ProviderContractError(
                f"{where}.grants.{service} is not monotonic: a role may only hold a grant "
                "if every higher-precedence role holds one"
            )
    return document


def _check_identity_roles(value: object, distribution_root: Path | None) -> None:
    where = "shared.identity.roles"
    roles = _identity_roles(value, where=where)
    # An installed project's process environment does not name its payload
    # root, so callers that know it pass it; the environment is the fallback.
    path = (distribution_root or config.distribution_root()) / IDENTITY_ROLES_PATH
    try:
        canonical = _identity_roles(yaml.safe_load(path.read_text(encoding="utf-8")), where=IDENTITY_ROLES_PATH)
    except (OSError, yaml.YAMLError) as exc:
        raise ProviderContractError(f"cannot read the canonical role model {path}: {exc}") from exc
    if roles != canonical:
        raise ProviderContractError(f"{where} differs from {IDENTITY_ROLES_PATH}; the role model is fixed per release")


_OIDC_IMPLEMENTATION = "identity.oidc"
_OIDC_FIELDS = {"issuer_url", "role_claim", "role_mapping", "clients"}
# Optional: `adapter` is provenance only, `capabilities` declares what this
# issuer lacks so consumers degrade (no admin API: `olf users` read-only).
_OIDC_OPTIONAL = {"adapter", "capabilities"}
_OIDC_CAPABILITIES = frozenset({"admin_api", "groups_in_token", "logout_endpoint"})
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
_SECRET_DATA_KEY = re.compile(r"[-._a-zA-Z0-9]+")
_OIDC_CLIENTS = ("perimeter", "superset", "openmetadata", "trino")


def _check_identity_oidc(identity: Mapping[str, Any], selected: IdentitySpec) -> None:
    """`identity.oidc`: the issuer seam of ADR 0014. Everything here is keyed by
    a canonical role or a consumer, never by an issuer product; credentials are
    Secret references only."""
    where = "shared.identity"
    present = _OIDC_FIELDS & set(identity)
    if selected.issuer == "external" and identity["implementation"] != _OIDC_IMPLEMENTATION:
        raise ProviderContractError(f"{where} must use implementation identity.oidc for an external spec.identity")
    if identity["implementation"] != _OIDC_IMPLEMENTATION:
        present = present | (_OIDC_OPTIONAL & set(identity))
        if present:
            raise ProviderContractError(f"{where} fields {sorted(present)!r} require implementation identity.oidc")
        return
    if present != _OIDC_FIELDS:
        raise ProviderContractError(f"{where} identity.oidc requires {sorted(_OIDC_FIELDS - present)!r}")
    if "adapter" in identity:
        _string(identity["adapter"], where=f"{where}.adapter")
    for capability, enabled in _mapping(identity.get("capabilities", {}), where=f"{where}.capabilities").items():
        if capability not in _OIDC_CAPABILITIES or not isinstance(enabled, bool):
            raise ProviderContractError(
                f"{where}.capabilities.{capability} must be a boolean named one of {sorted(_OIDC_CAPABILITIES)!r}"
            )
    _oidc_issuer_url(identity["issuer_url"], where=f"{where}.issuer_url")
    _string(identity["role_claim"], where=f"{where}.role_claim")
    roles = identity["roles"]["precedence"]
    mapping = _mapping(identity["role_mapping"], where=f"{where}.role_mapping")
    if not mapping:
        raise ProviderContractError(f"{where}.role_mapping must map at least one canonical role")
    for role, values in mapping.items():
        if role not in roles:
            raise ProviderContractError(f"{where}.role_mapping names unknown role {role!r}")
        if not isinstance(values, list) or not values or len(set(map(str, values))) != len(values):
            raise ProviderContractError(f"{where}.role_mapping.{role} must be a non-empty list of unique claim values")
        for value in values:
            _string(value, where=f"{where}.role_mapping.{role} entry")
    claims = [v for values in mapping.values() for v in values]
    if len(set(claims)) != len(claims):
        raise ProviderContractError(f"{where}.role_mapping must not map one claim value to more than one role")
    clients = _fields(identity["clients"], where=f"{where}.clients", required=set(_OIDC_CLIENTS))
    for name, client in clients.items():
        client_where = f"{where}.clients.{name}"
        document = _fields(client, where=client_where, required={"client_id", "secret_ref"})
        _string(document["client_id"], where=f"{client_where}.client_id")
        secret_ref = _fields(document["secret_ref"], where=f"{client_where}.secret_ref", required={"name", "key"})
        for field, valid in (("name", _DNS_SUBDOMAIN), ("key", _SECRET_DATA_KEY)):
            ref = _string(secret_ref[field], where=f"{client_where}.secret_ref.{field}")
            long_label = field == "name" and any(len(label) > 63 for label in ref.split("."))
            if len(ref) > 253 or not valid.fullmatch(ref) or ref in (".", "..") or long_label:
                raise ProviderContractError(
                    f"{client_where}.secret_ref.{field} is not a valid Kubernetes Secret {field}"
                )
    if selected.issuer == "keycloak" and identity.get("adapter") != "keycloak":
        raise ProviderContractError(f"{where} must come from the keycloak adapter when spec.identity is keycloak")
    deployed = (identity["issuer_url"], identity["role_claim"], {r: tuple(v) for r, v in mapping.items()})
    declared = (selected.issuer_url, selected.role_claim, selected.role_mapping)
    # keycloak compares only the fields the profile supplied; external compares all
    if any(
        (selected.issuer == "external" or d is not None) and d != c for c, d in zip(deployed, declared, strict=True)
    ):
        raise ProviderContractError(
            f"{where} issuer_url, role_claim and role_mapping must match the profile's external spec.identity"
        )


def _parse_access_ingress(access: Mapping[str, Any], *, service_refs: set[str], topology: DeploymentTopology) -> None:
    present = _ACCESS_INGRESS_FIELDS & set(access)
    if not present:
        if access["implementation"] == "access.ingress":
            raise ProviderContractError(f"shared.access access.ingress requires {sorted(_ACCESS_INGRESS_FIELDS)!r}")
        return
    if present != _ACCESS_INGRESS_FIELDS:
        raise ProviderContractError(
            "shared.access ingress fields must be declared together; "
            f"missing {sorted(_ACCESS_INGRESS_FIELDS - present)!r}"
        )
    base_domain = _string(access["base_domain"], where="shared.access.base_domain")
    if base_domain != topology.access.base_domain:
        raise ProviderContractError(
            f"shared.access.base_domain {base_domain!r} does not match the profile's {topology.access.base_domain!r}"
        )
    if _string(access["issuer"], where="shared.access.issuer") != topology.access.issuer:
        raise ProviderContractError(f"shared.access.issuer must match the profile's {topology.access.issuer!r}")
    if _string(access["tls_mode"], where="shared.access.tls_mode") not in _TLS_MODES:
        raise ProviderContractError(f"shared.access.tls_mode must be one of {sorted(_TLS_MODES)!r}")
    hosts: set[str] = set()
    for ref, route in _mapping(access["routes"], where="shared.access.routes").items():
        where = f"shared.access.routes[{ref!r}]"
        document = _fields(route, where=where, required={"url", "enabled", "exposure"})
        if ref not in service_refs:
            raise ProviderContractError(f"{where} does not resolve to an enabled service")
        if document["exposure"] not in _ROUTE_EXPOSURES:
            raise ProviderContractError(f"{where}.exposure must be one of {sorted(_ROUTE_EXPOSURES)!r}")
        if document["exposure"] == "user-facing" and ref.rsplit("/", 1)[-1] not in _USER_FACING_SERVICES:
            raise ProviderContractError(f"{where} is an internal endpoint and cannot be user-facing")
        if not isinstance(document["enabled"], bool):
            raise ProviderContractError(f"{where}.enabled must be a boolean")
        url = _absolute_http_uri(document["url"], where=f"{where}.url")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        # One host per service: a path or port would let two services share a host.
        if (
            parts.scheme != "https"
            or parts.port
            or parts.path not in ("", "/")
            or parts.query
            or not _BASE_DOMAIN_PATTERN.fullmatch(host)
        ):
            raise ProviderContractError(f"{where}.url must be https://<host>.{base_domain}")
        # ADR 0013: a stage route carries its own stage label, a shared route none,
        # so a DEV backend can never answer on a PROD or shared hostname.
        scope = f"{ref.split('/')[1]}." if ref.startswith("stage/") else ""
        suffix = f".{scope}{base_domain}".lower()
        if ref == _PORTAL_REF:
            if host != base_domain.lower():
                raise ProviderContractError(f"{where}.url must be https://{base_domain}")
        elif not host.endswith(suffix) or "." in host[: -len(suffix)]:
            raise ProviderContractError(f"{where}.url must be https://<service>{suffix}")
        if host in hosts:
            raise ProviderContractError(f"{where}.url host {host!r} is already routed to another service")
        hosts.add(host)


def _parse_shared(
    value: object, distribution_root: Path | None, topology: DeploymentTopology
) -> SharedPlatformContract:
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
    optional = {"catalog_service", "governance_service", "portal"}
    document = _fields(value, where="shared", required=required, optional=optional)
    parsed: dict[str, Mapping[str, Any]] = {}
    for name, binding in document.items():
        parsed[name] = _fields(
            binding,
            where=f"shared.{name}",
            required={"ref", "implementation"},
            optional=(_ACCESS_INGRESS_FIELDS if name == "access" else set())
            | ({"roles"} | _OIDC_FIELDS | _OIDC_OPTIONAL if name == "identity" else set())
            | {
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
    if "roles" not in parsed["identity"]:
        raise ProviderContractError("shared.identity is missing required field 'roles'")
    _check_identity_roles(parsed["identity"]["roles"], distribution_root)
    _check_identity_oidc(parsed["identity"], topology.identity)
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


def _parse_v3(
    payload: Mapping[str, Any], topology: DeploymentTopology | None, distribution_root: Path | None = None
) -> ProviderContracts:
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
    shared = _parse_shared(document["shared"], distribution_root, topology)
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
    catalog_ids: set[tuple[str, str]] = set()
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
    service_refs = {binding["ref"] for name, binding in shared.values.items() if name not in _NON_SERVICE_BINDINGS}
    for stage in stages.values():
        service_refs.add(stage.orchestration["service_ref"])
        if stage.reporting is not None:
            service_refs.add(stage.reporting["service_ref"])
    _parse_access_ingress(shared.values["access"], service_refs=service_refs, topology=topology)
    return ProviderContracts(
        schema_version=V3_SCHEMA_VERSION,
        deployment=_frozen(deployment),
        shared=shared,
        stages=MappingProxyType(stages),
    )
