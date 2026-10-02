"""Field, reference, and URI validators shared by the provider-contract readers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from olf.deployment.context import Provider
from olf.profile import StageName

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


_SERVICE_NAME_PATTERN = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
_PYTHON_MODULE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


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
