"""Typed Deployment Profile v1 and the resolver into one effective topology.

The project-root ``openlakeforge.yaml`` describes product intent -- provider,
lifecycle stage, and preset -- never Terraform, Helm, or Kubernetes
implementation details (ADR 0011). ``DeploymentProfile`` models what the user
wrote; ``DeploymentTopology`` is the separately typed, deterministically
resolved effective shape. Neither type derives concrete endpoints,
namespaces, or Helm/Terraform inputs -- that mapping is the provider-contract
resolver (#153) and the stage-aware platform root (#133).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, is_dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import yaml

from olf.common import config
from olf.common.contract_validation import ProviderContractError, _oidc_issuer_url

PROFILE_API_VERSION = "openlakeforge.io/v1alpha1"
PROFILE_KIND = "DeploymentProfile"
# A profile name reaches Kubernetes as the `openlakeforge.io/profile` label
# value on every namespace this deployment owns, and teardown discovers those
# namespaces by selecting on it. Label values must end in an alphanumeric
# character and cannot exceed 63 characters, so a name that only satisfies the
# looser identifier shape would be accepted here and then fail the apply that
# creates the namespaces.
_IDENTIFIER_PATTERN_SOURCE = r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$"
_IDENTIFIER_PATTERN = re.compile(_IDENTIFIER_PATTERN_SOURCE)

_ENVELOPE_FIELDS = {"apiVersion", "kind", "metadata", "spec"}
_METADATA_FIELDS = {"name"}
_SPEC_FIELDS = {"provider", "preset", "stages", "access", "identity", "project", "query", "storage"}
_SIZE_BLOCK_FIELDS = {"size"}
_PROVIDER_FIELDS = {"type", "region"}
_ACCESS_FIELDS = {"base_domain", "issuer"}
_IDENTITY_FIELDS = {"issuer", "issuer_url", "role_claim", "role_mapping", "client_ids", "github"}
_IDENTITY_ISSUERS = ("external", "github")
_GITHUB_FIELDS = {"organization", "teams", "oauth_app_secret_ref", "host"}
_GITHUB_REQUIRED = _GITHUB_FIELDS - {"host"}
_GITHUB_SECRET_REF_FIELDS = {"name", "client_id_key", "client_secret_key"}
_GITHUB_ORG = re.compile(r"[A-Za-z0-9-]+")
_GITHUB_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_HOST_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_PATTERN = re.compile(rf"{_HOST_LABEL}(\.{_HOST_LABEL})*")
# Kubernetes Secret metadata.name is a DNS subdomain (labels of at most 63
# characters) and a data key is `[-._a-zA-Z0-9]+`, both at most 253 characters.
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
_SECRET_DATA_KEY = re.compile(r"[-._a-zA-Z0-9]+")
# Dot-separated DNS labels; every route host is `<service>[.<stage>].<base_domain>`.
_BASE_DOMAIN_PATTERN = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
# spec.project (ADR 0011): a source URL (provenance) and an image repository, never a credential.
_PROJECT_FIELDS = {"repository", "image"}
_PROJECT_IMAGE_FIELDS = {"repository", "pull_secret_ref"}
_PULL_SECRET_REF_FIELDS = {"name"}
_REGISTRY_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_REPOSITORY_NAME = r"[a-z0-9]+([._-][a-z0-9]+)*"
# `host[:port]/path`, no tag or digest. The registry host is mandatory: a bare `acme/code` means Docker Hub
# implicitly, and a profile that silently pulls from there is the ambiguity this contract rejects.
_IMAGE_REPOSITORY_PATTERN_SOURCE = (
    rf"((localhost|{_REGISTRY_LABEL}(\.{_REGISTRY_LABEL})+)(:[0-9]{{1,5}})?|{_REGISTRY_LABEL}:[0-9]{{1,5}})"
    rf"/{_REPOSITORY_NAME}(/{_REPOSITORY_NAME})*"
)
_IMAGE_REPOSITORY_PATTERN = re.compile(_IMAGE_REPOSITORY_PATTERN_SOURCE)
# The package the release publishes: `<IMAGE_NAMESPACE>/project-code` in release.yml, namespaced by the repository.
PUBLIC_EXAMPLE_IMAGE_REPOSITORY = "ghcr.io/openlakeforge/openlakeforge/project-code"
_STAGE_FIELDS = {"enabled", "capabilities"}
_CAPABILITIES_FIELDS = {"analytics", "governance"}

_SHARED_SERVICES = ("catalog", "governance", "metadata_database", "query")
_STAGE_SERVICES = ("orchestration", "reporting")


class DeploymentProfileError(ValueError):
    """Raised when a v1alpha1 Deployment Profile is invalid."""


class StageName(StrEnum):
    DEV = "dev"
    UAT = "uat"
    PROD = "prod"


class Preset(StrEnum):
    SLIM = "slim"
    FULL = "full"


class Provider(StrEnum):
    LOCAL = "local"
    AWS = "aws"
    AZURE = "azure"


class Size(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# Sizes with a tested backend. A tier outside this set is rejected rather than accepted as a no-op (ADR 0011);
# growing it is the change that implements the tier. The value names the issue that will do so.
_IMPLEMENTED_SIZES = {"query": (Size.SMALL,), "storage": (Size.SMALL, Size.MEDIUM, Size.LARGE)}
_SIZE_TRACKER = {"query": "#324 (Trino workers)", "storage": "#326 (SeaweedFS volumes)"}


@dataclass(frozen=True)
class ProviderSpec:
    type: Provider
    region: str | None = None


@dataclass(frozen=True)
class AccessSpec:
    """Where user-facing services are published (ADR 0013). Declaring the block
    turns the ingress on; the defaults are the local install: `*.olf.localhost`
    names and a local CA. Without the block there is no ingress at all."""

    base_domain: str = "olf.localhost"
    issuer: str = "local-ca"


def is_secret_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 253
        and _DNS_SUBDOMAIN.fullmatch(value) is not None
        and all(len(label) <= 63 for label in value.split("."))
    )


def is_secret_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 253
        and _SECRET_DATA_KEY.fullmatch(value) is not None
        and value not in (".", "..")
    )


@dataclass(frozen=True)
class GitHubOAuthRef:
    """The Secret holding the GitHub OAuth App's client id and secret, created
    out-of-band. The profile names it and never carries a value."""

    name: str
    client_id_key: str = "client-id"
    client_secret_key: str = "client-secret"


@dataclass(frozen=True)
class GitHubSpec:
    """People and roles live in a GitHub organisation (ADR 0014); the profile only
    says which team holds which canonical role."""

    organization: str
    teams: Mapping[str, tuple[str, ...]]
    oauth_app_secret_ref: GitHubOAuthRef
    host: str | None = None  # GitHub Enterprise Server hostname; None is github.com


@dataclass(frozen=True)
class IdentitySpec:
    """Which OIDC issuer signs people in (ADR 0014 seam 2). `github` is the
    Dex adapter OpenLakeForge deploys; `external` points at an existing
    issuer and must say how its claims map to canonical roles. Credentials never
    appear here: client secrets are Secret references in the provider contract."""

    issuer: str
    issuer_url: str | None = None
    role_claim: str | None = None
    role_mapping: Mapping[str, tuple[str, ...]] | None = None
    # Issuer-assigned client ids (Cognito, Entra), by consumer; absent = the consumer name.
    client_ids: Mapping[str, str] | None = None
    # For issuer `github`, role_claim and role_mapping above are derived from this, never written.
    github: GitHubSpec | None = None


@dataclass(frozen=True)
class ProjectImageSpec:
    """Where the customer's project-code image lives. `pull_secret_name` names a
    Kubernetes `kubernetes.io/dockerconfigjson` Secret created out-of-band; the
    profile never carries the credential (#540 consumes the reference)."""

    repository: str
    pull_secret_name: str | None = None


@dataclass(frozen=True)
class ProjectSpec:
    """The customer's project, declared (ADR 0011). `repository` is provenance
    only: `olf` builds the checked-out directory, never a fetched ref."""

    repository: str | None = None
    image: ProjectImageSpec | None = None


@dataclass(frozen=True)
class ResolvedProject:
    """Where the project-code image comes from. `source` is the precedence tier
    that won; `image_repository is None` means the provider registry (ECR/ACR),
    known only once the foundation exists."""

    source: str  # profile | environment | provider-default | public-example
    repository: str | None
    image_repository: str | None
    pull_secret_name: str | None = None


@dataclass(frozen=True)
class StageCapabilities:
    analytics: bool = False
    governance: bool = False


@dataclass(frozen=True)
class StageSpec:
    """One stage as the user wrote it. ``capabilities is None`` means the
    user said nothing -- distinct from writing ``false`` -- so preset
    defaults apply only to the former in `resolve_topology`."""

    name: StageName
    enabled: bool = True
    capabilities: StageCapabilities | None = None


@dataclass(frozen=True)
class DeploymentProfile:
    name: str
    provider: ProviderSpec
    preset: Preset
    stages: tuple[StageSpec, ...]
    access: AccessSpec | None = None
    identity: IdentitySpec | None = None
    project: ProjectSpec | None = None
    query_size: Size = Size.SMALL
    storage_size: Size = Size.SMALL

    def stage(self, name: StageName) -> StageSpec | None:
        return next((stage for stage in self.stages if stage.name == name), None)


@dataclass(frozen=True)
class ResolvedStage:
    name: StageName
    enabled: bool
    capabilities: StageCapabilities

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name.value,
            "enabled": self.enabled,
            "capabilities": {
                "analytics": self.capabilities.analytics,
                "governance": self.capabilities.governance,
            },
        }


@dataclass(frozen=True)
class DeploymentTopology:
    """One effective, fully resolved topology. Always a separate object from
    the `DeploymentProfile` it was resolved from -- never mutated in place."""

    profile_name: str
    provider: Provider
    region: str | None
    preset: Preset
    stages: tuple[ResolvedStage, ...]
    shared_services: tuple[str, ...] = _SHARED_SERVICES
    stage_services: tuple[str, ...] = _STAGE_SERVICES
    access: AccessSpec | None = None
    identity: IdentitySpec | None = None
    project: ProjectSpec | None = None
    query_size: Size = Size.SMALL
    storage_size: Size = Size.SMALL

    def stage(self, name: StageName) -> ResolvedStage | None:
        return next((stage for stage in self.stages if stage.name == name), None)

    def render_json(self) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "profile_name": self.profile_name,
                "provider": self.provider.value,
                "region": self.region,
                "preset": self.preset.value,
                "stages": [stage.as_dict() for stage in self.stages],
                "shared_services": list(self.shared_services),
                "stage_services": list(self.stage_services),
                "access": None if self.access is None else vars(self.access),
                "identity": None if self.identity is None else vars(self.identity),
                "query": {"size": self.query_size.value},
                "storage": {"size": self.storage_size.value},
            },
            sort_keys=True,
            default=_json_default,
        )


def _json_default(value: Any) -> Any:
    # json recurses into the result, so a shallow view suffices; asdict would deepcopy a read-only MappingProxyType.
    return vars(value) if is_dataclass(value) and not isinstance(value, type) else dict(value)


def _identifier(value: object, *, field: str, source: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise DeploymentProfileError(f"{source}: {field} must match '{_IDENTIFIER_PATTERN_SOURCE}'")
    return value


def _bool(value: object, *, field: str, source: str) -> bool:
    if not isinstance(value, bool):
        raise DeploymentProfileError(f"{source}: {field} must be a boolean")
    return value


def _forbid_unexpected(document: Mapping[str, Any], allowed: set[str], *, where: str) -> None:
    unexpected = set(document) - allowed
    if unexpected:
        raise DeploymentProfileError(f"{where}: must not contain {sorted(unexpected)!r}")


def _validate_capabilities(document: object, *, field: str, source: str) -> StageCapabilities:
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{source}: {field} must be an object")
    _forbid_unexpected(document, _CAPABILITIES_FIELDS, where=f"{source}: {field}")
    analytics = _bool(document.get("analytics", False), field=f"{field}.analytics", source=source)
    governance = _bool(document.get("governance", False), field=f"{field}.governance", source=source)
    return StageCapabilities(analytics=analytics, governance=governance)


def _validate_stage(name: str, document: object, *, source: str) -> StageSpec:
    try:
        stage_name = StageName(name)
    except ValueError as exc:
        raise DeploymentProfileError(
            f"{source}: spec.stages: unknown stage {name!r} "
            f"(expected one of {[member.value for member in StageName]!r})"
        ) from exc
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{source}: spec.stages.{name} must be an object")
    _forbid_unexpected(document, _STAGE_FIELDS, where=f"{source}: spec.stages.{name}")
    enabled = _bool(document.get("enabled", True), field=f"spec.stages.{name}.enabled", source=source)
    capabilities = None
    if "capabilities" in document:
        capabilities = _validate_capabilities(
            document["capabilities"], field=f"spec.stages.{name}.capabilities", source=source
        )
    return StageSpec(name=stage_name, enabled=enabled, capabilities=capabilities)


def _validate_provider(document: object, *, source: str) -> ProviderSpec:
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{source}: spec.provider must be an object")
    _forbid_unexpected(document, _PROVIDER_FIELDS, where=f"{source}: spec.provider")
    if "type" not in document:
        raise DeploymentProfileError(f"{source}: spec.provider: missing required field 'type'")
    try:
        provider = Provider(document["type"])
    except ValueError as exc:
        raise DeploymentProfileError(
            f"{source}: spec.provider.type must be one of {[member.value for member in Provider]!r}"
        ) from exc
    region = document.get("region")
    if region is not None and not isinstance(region, str):
        raise DeploymentProfileError(f"{source}: spec.provider.region must be a string")
    if provider == Provider.LOCAL and region is not None:
        raise DeploymentProfileError(
            f"{source}: spec.provider.region must not be set when spec.provider.type is 'local'"
        )
    return ProviderSpec(type=provider, region=region)


def _validate_size(spec: Mapping[str, Any], block: str, *, source: str) -> Size:
    """`spec.query.size` / `spec.storage.size`: optional, `small` by default, and only tiers with a tested backend."""
    if block not in spec:
        return Size.SMALL
    document = spec[block]
    where = f"{source}: spec.{block}"
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{where} must be an object")
    _forbid_unexpected(document, _SIZE_BLOCK_FIELDS, where=where)
    try:
        size = Size(document.get("size", Size.SMALL))
    except ValueError as exc:
        raise DeploymentProfileError(f"{where}.size must be one of {[member.value for member in Size]!r}") from exc
    if size not in _IMPLEMENTED_SIZES[block]:
        raise DeploymentProfileError(
            f"{where}.size {size.value!r} has no implementation yet and is not silently accepted: "
            f"use {[member.value for member in _IMPLEMENTED_SIZES[block]]!r} until {_SIZE_TRACKER[block]} lands"
        )
    return size


def _validate_access(document: object, *, source: str, provider: Provider) -> AccessSpec:
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{source}: spec.access must be an object")
    _forbid_unexpected(document, _ACCESS_FIELDS, where=f"{source}: spec.access")
    defaults = AccessSpec()
    base_domain = document.get("base_domain", defaults.base_domain)
    if not isinstance(base_domain, str) or not _BASE_DOMAIN_PATTERN.fullmatch(base_domain):
        raise DeploymentProfileError(f"{source}: spec.access.base_domain must be a lowercase DNS name with a dot")
    issuer = _identifier(document.get("issuer", defaults.issuer), field="spec.access.issuer", source=source)
    # Plain HTTP is safe only where the browser treats the host as loopback and a
    # secure context: *.localhost, on the local kind cluster (ADR 0013).
    if issuer == "none" and (provider != Provider.LOCAL or not base_domain.endswith(".localhost")):
        raise DeploymentProfileError(
            f"{source}: spec.access.issuer 'none' (plain HTTP) requires provider 'local' "
            "and a base_domain ending in '.localhost'"
        )
    return AccessSpec(base_domain=base_domain, issuer=issuer)


def _known_roles(*, where: str, distribution_root: Path | None) -> list[str]:
    # Fail closed on a role the product does not define (release/identity-roles.yaml).
    roles_file = (distribution_root or config.distribution_root()) / "release/identity-roles.yaml"
    try:
        return list(yaml.safe_load(roles_file.read_text("utf-8"))["precedence"])
    except OSError as exc:
        raise DeploymentProfileError(f"{where}: cannot read {roles_file}: {exc.strerror}") from exc


def _validate_github(document: object, *, where: str, distribution_root: Path | None) -> GitHubSpec:
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{where} must be an object")
    _forbid_unexpected(document, _GITHUB_FIELDS, where=where)
    missing = sorted(_GITHUB_REQUIRED - set(document))
    if missing:
        raise DeploymentProfileError(f"{where}: missing required field(s) {missing!r}")
    org = document["organization"]
    if not isinstance(org, str) or not _GITHUB_ORG.fullmatch(org):
        raise DeploymentProfileError(f"{where}.organization must be a GitHub organisation name")
    # Kept as written: GitHub preserves the case of an organisation login and
    # Dex compares a team's organization.login with this value exactly, so a
    # case variant refuses every member ("user in org but no teams"). Slugs,
    # by contrast, are always lowercase on GitHub and are lowercased below.
    raw = document["teams"]
    if not isinstance(raw, Mapping) or not raw:
        raise DeploymentProfileError(f"{where}.teams must be a non-empty object")
    roles = _known_roles(where=f"{where}.teams", distribution_root=distribution_root)
    lowered: dict[str, list[str]] = {}
    for role, slugs in raw.items():
        if role not in roles:
            raise DeploymentProfileError(f"{where}.teams names unknown role {role!r}")
        if not isinstance(slugs, list) or not slugs or not all(isinstance(s, str) for s in slugs):
            raise DeploymentProfileError(f"{where}.teams.{role} must be a non-empty list of team slugs")
        slugs = [s.lower() for s in slugs]
        bad = [s for s in slugs if not _GITHUB_SLUG.fullmatch(s)]
        if bad:
            raise DeploymentProfileError(
                f"{where}.teams.{role}: {bad!r} is not a team slug; use the slug as GitHub shows it in the team URL"
            )
        lowered[role] = slugs
        if len(set(slugs)) != len(slugs):
            raise DeploymentProfileError(f"{where}.teams.{role} must not repeat a team slug")
    every = [s for slugs in lowered.values() for s in slugs]
    if len(set(every)) != len(every):
        raise DeploymentProfileError(f"{where}.teams must not map one team to more than one role")
    ref = document["oauth_app_secret_ref"]
    ref_where = f"{where}.oauth_app_secret_ref"
    if not isinstance(ref, Mapping):
        raise DeploymentProfileError(f"{ref_where} must be an object")
    _forbid_unexpected(ref, _GITHUB_SECRET_REF_FIELDS, where=ref_where)
    if "name" not in ref:
        raise DeploymentProfileError(f"{ref_where}: missing required field 'name'")
    oauth = GitHubOAuthRef(**ref)
    checks = (("name", is_secret_name), ("client_id_key", is_secret_key), ("client_secret_key", is_secret_key))
    for field, valid in checks:
        if not valid(getattr(oauth, field)):
            raise DeploymentProfileError(f"{ref_where}.{field} is not a valid Kubernetes Secret {field.split('_')[-1]}")
    if oauth.client_id_key == oauth.client_secret_key:
        raise DeploymentProfileError(f"{ref_where}: client_id_key and client_secret_key must differ")
    host = document.get("host")
    if host is not None and (not isinstance(host, str) or len(host) > 253 or not _HOSTNAME_PATTERN.fullmatch(host)):
        raise DeploymentProfileError(f"{where}.host must be a hostname, without a scheme or path")
    teams = MappingProxyType({role: tuple(slugs) for role, slugs in lowered.items()})
    return GitHubSpec(organization=org, teams=teams, oauth_app_secret_ref=oauth, host=host)


def _image_repository(value: object, *, where: str) -> str:
    if not isinstance(value, str) or len(value) > 255 or not _IMAGE_REPOSITORY_PATTERN.fullmatch(value):
        raise DeploymentProfileError(
            f"{where} must be a registry repository such as ghcr.io/acme/lakehouse-code: "
            "host first, lowercase, no tag, digest or credentials"
        )
    return value


def _validate_project(document: object, *, source: str, provider: Provider) -> ProjectSpec:
    where = f"{source}: spec.project"
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{where} must be an object")
    _forbid_unexpected(document, _PROJECT_FIELDS, where=where)
    if not document:
        raise DeploymentProfileError(f"{where} must declare 'repository' or 'image'; omit the block for the example")
    repository = document.get("repository")
    if "repository" in document:
        try:
            parts = urlsplit(repository) if isinstance(repository, str) else None
        except ValueError:
            parts = None
        # No userinfo over https, and never a password: a token in a URL is a credential in the profile.
        if (
            parts is None
            or parts.scheme not in ("https", "ssh")
            or not parts.hostname
            or parts.password is not None
            or (parts.scheme == "https" and parts.username is not None)
            or parts.query
            or parts.fragment
            or not parts.path.strip("/")
            or re.search(r"\s", str(repository))
        ):
            raise DeploymentProfileError(
                f"{where}.repository must be an https:// or ssh:// URL without credentials, query or fragment"
            )
    image = None
    if "image" in document:
        raw = document["image"]
        if not isinstance(raw, Mapping):
            raise DeploymentProfileError(f"{where}.image must be an object")
        _forbid_unexpected(raw, _PROJECT_IMAGE_FIELDS, where=f"{where}.image")
        if "repository" not in raw:
            raise DeploymentProfileError(f"{where}.image: missing required field 'repository'")
        pull_secret_name = None
        if "pull_secret_ref" in raw:
            ref = raw["pull_secret_ref"]
            if not isinstance(ref, Mapping):
                raise DeploymentProfileError(f"{where}.image.pull_secret_ref must be an object")
            _forbid_unexpected(ref, _PULL_SECRET_REF_FIELDS, where=f"{where}.image.pull_secret_ref")
            if not is_secret_name(ref.get("name")):
                raise DeploymentProfileError(
                    f"{where}.image.pull_secret_ref.name is not a valid Kubernetes Secret name"
                )
            pull_secret_name = ref["name"]
        image = ProjectImageSpec(
            repository=_image_repository(raw["repository"], where=f"{where}.image.repository"),
            pull_secret_name=pull_secret_name,
        )
    # The clouds have a registry of their own to default to; kind has none, so a local project must name one.
    if provider == Provider.LOCAL and image is None:
        raise DeploymentProfileError(f"{where}.image is required on provider 'local' (there is no default registry)")
    return ProjectSpec(repository=repository, image=image)


def _validate_identity(document: object, *, source: str, distribution_root: Path | None = None) -> IdentitySpec:
    where = f"{source}: spec.identity"
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{where} must be an object")
    _forbid_unexpected(document, _IDENTITY_FIELDS, where=where)
    issuer = document.get("issuer")
    if issuer not in _IDENTITY_ISSUERS:
        raise DeploymentProfileError(f"{where}.issuer must be one of {list(_IDENTITY_ISSUERS)!r}")
    if issuer == "external":
        missing = sorted({"issuer_url", "role_claim", "role_mapping"} - set(document))
        if missing:
            raise DeploymentProfileError(f"{where}: issuer 'external' requires {missing!r}")
    github = None
    if issuer == "github":
        if "github" not in document:
            raise DeploymentProfileError(f"{where}: issuer 'github' requires 'github'")
        # The mapping is derived from `github`, so a second source could only disagree with it.
        applicable = sorted({"issuer_url", "role_claim", "role_mapping"} & set(document))
        if applicable:
            raise DeploymentProfileError(f"{where}: issuer 'github' derives its claims; remove {applicable!r}")
        github = _validate_github(document["github"], where=f"{where}.github", distribution_root=distribution_root)
    elif "github" in document:
        raise DeploymentProfileError(f"{where}.github requires issuer 'github'")
    issuer_url = document.get("issuer_url")
    if "issuer_url" in document:  # by key: an explicit null is invalid, not omitted
        try:
            _oidc_issuer_url(issuer_url, where=f"{where}.issuer_url")
        except ProviderContractError as exc:
            raise DeploymentProfileError(str(exc)) from exc
    role_claim = document.get("role_claim")
    if "role_claim" in document and (not isinstance(role_claim, str) or not role_claim):
        raise DeploymentProfileError(f"{where}.role_claim must be a non-empty string")
    mapping = None
    if "role_mapping" in document:
        raw = document["role_mapping"]
        if not isinstance(raw, Mapping) or not raw:
            raise DeploymentProfileError(f"{where}.role_mapping must be a non-empty object")
        roles = _known_roles(where=f"{where}.role_mapping", distribution_root=distribution_root)
        for role, values in raw.items():
            if role not in roles:
                raise DeploymentProfileError(f"{where}.role_mapping names unknown role {role!r}")
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                raise DeploymentProfileError(f"{where}.role_mapping.{role} must be a non-empty list of strings")
            if len(set(values)) != len(values):
                raise DeploymentProfileError(f"{where}.role_mapping.{role} must not repeat a claim value")
        claims = [v for values in raw.values() for v in values]
        if len(set(claims)) != len(claims):
            raise DeploymentProfileError(f"{where}.role_mapping must not map one claim value to more than one role")
        mapping = MappingProxyType({role: tuple(values) for role, values in raw.items()})
    client_ids = document.get("client_ids")
    if "client_ids" in document:
        if issuer != "external":
            raise DeploymentProfileError(f"{where}.client_ids requires issuer 'external'")
        consumers = {"perimeter", "superset", "openmetadata", "trino", "olf-cli"}
        if not isinstance(client_ids, Mapping) or not client_ids or set(client_ids) - consumers:
            raise DeploymentProfileError(f"{where}.client_ids must be a non-empty object keyed by {sorted(consumers)}")
        if not all(isinstance(v, str) and v for v in client_ids.values()):
            raise DeploymentProfileError(f"{where}.client_ids values must be non-empty strings")
        client_ids = MappingProxyType(dict(client_ids))
    if github is not None:
        role_claim = "groups"
        mapping = MappingProxyType(
            {role: tuple(f"{github.organization}:{slug}" for slug in slugs) for role, slugs in github.teams.items()}
        )
    return IdentitySpec(
        issuer=issuer,
        issuer_url=issuer_url,
        role_claim=role_claim,
        role_mapping=mapping,
        client_ids=client_ids,
        github=github,
    )


def validate_deployment_profile(
    document: Mapping[str, Any], *, source: str = "openlakeforge.yaml", distribution_root: Path | None = None
) -> DeploymentProfile:
    """Validate a v1alpha1 Deployment Profile envelope and build its typed
    model. Every rejection is fail-closed: unknown fields at any level,
    unknown stage names, an unsupported apiVersion/kind/preset/provider type,
    no stage enabled at all, and a UAT/PROD stage enabled while DEV is
    disabled (every promotion in the v0.3 model sources from DEV)."""
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{source}: profile must contain a YAML object")
    _forbid_unexpected(document, _ENVELOPE_FIELDS, where=source)
    if document.get("apiVersion") != PROFILE_API_VERSION:
        raise DeploymentProfileError(
            f"{source}: unsupported apiVersion {document.get('apiVersion')!r}; expected {PROFILE_API_VERSION!r}"
        )
    if document.get("kind") != PROFILE_KIND:
        raise DeploymentProfileError(f"{source}: kind must be {PROFILE_KIND!r}")
    for field in ("metadata", "spec"):
        if field not in document:
            raise DeploymentProfileError(f"{source}: missing required field {field!r}")

    metadata = document["metadata"]
    if not isinstance(metadata, Mapping):
        raise DeploymentProfileError(f"{source}: metadata must be an object")
    _forbid_unexpected(metadata, _METADATA_FIELDS, where=f"{source}: metadata")
    if "name" not in metadata:
        raise DeploymentProfileError(f"{source}: metadata: missing required field 'name'")
    name = _identifier(metadata["name"], field="metadata.name", source=source)

    spec = document["spec"]
    if not isinstance(spec, Mapping):
        raise DeploymentProfileError(f"{source}: spec must be an object")
    _forbid_unexpected(spec, _SPEC_FIELDS, where=f"{source}: spec")
    for field in ("provider", "preset", "stages"):
        if field not in spec:
            raise DeploymentProfileError(f"{source}: spec: missing required field {field!r}")

    provider = _validate_provider(spec["provider"], source=source)

    try:
        preset = Preset(spec["preset"])
    except ValueError as exc:
        raise DeploymentProfileError(
            f"{source}: spec.preset must be one of {[member.value for member in Preset]!r}"
        ) from exc

    stages_document = spec["stages"]
    if not isinstance(stages_document, Mapping) or not stages_document:
        raise DeploymentProfileError(f"{source}: spec.stages must be a non-empty object")
    stages = tuple(
        _validate_stage(stage_name, stage_document, source=source)
        for stage_name, stage_document in stages_document.items()
    )

    if not any(stage.enabled for stage in stages):
        raise DeploymentProfileError(f"{source}: spec.stages: at least one stage must be enabled")
    dev = next((stage for stage in stages if stage.name == StageName.DEV), None)
    dev_enabled = dev is not None and dev.enabled
    for stage in stages:
        if stage.enabled and stage.name != StageName.DEV and not dev_enabled:
            raise DeploymentProfileError(
                f"{source}: spec.stages.{stage.name.value} cannot be enabled while 'dev' is disabled "
                "(every promotion sources from DEV)"
            )

    # Block presence is the switch (ADR 0013): `access: {}` is a valid way to ask for the defaults.
    access = _validate_access(spec["access"], source=source, provider=provider.type) if "access" in spec else None
    identity = (
        _validate_identity(spec["identity"], source=source, distribution_root=distribution_root)
        if "identity" in spec
        else None
    )
    project = _validate_project(spec["project"], source=source, provider=provider.type) if "project" in spec else None
    if provider.type == Provider.LOCAL and identity is not None and access is None:
        raise DeploymentProfileError(
            f"{source}: spec.identity requires spec.access on the local provider (sign-in needs the route hostnames)"
        )
    # No identity block means authentication off (ADR 0014): admin on every
    # service, so it is allowed only where nothing is reachable beyond the
    # operator's machine. The AWS and Azure roots publish no routes, so
    # without spec.access they are reached only through `olf infra forward`; a
    # cloud spec.access asks for routes and so needs identity first.
    published = access is not None and (
        provider.type != Provider.LOCAL or not access.base_domain.endswith(".localhost")
    )
    if identity is None and published:
        raise DeploymentProfileError(
            f"{source}: a profile without spec.identity (authentication off) publishes nothing beyond this machine: "
            "use no spec.access, or on provider 'local' a base_domain ending in '.localhost'"
        )
    if provider.type == Provider.AWS and "storage" in spec:
        raise DeploymentProfileError(
            f"{source}: spec.storage is not available on provider 'aws': object storage is managed S3 there, "
            "so there is no self-hosted volume to size; remove spec.storage"
        )
    return DeploymentProfile(
        name=name,
        provider=provider,
        preset=preset,
        stages=stages,
        access=access,
        identity=identity,
        project=project,
        query_size=_validate_size(spec, "query", source=source),
        storage_size=_validate_size(spec, "storage", source=source),
    )


def load_deployment_profile(path: str | Path, *, distribution_root: Path | None = None) -> DeploymentProfile:
    """Load and validate the v1alpha1 Deployment Profile at ``path``."""
    source = str(path)
    with Path(path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{source}: profile must contain a YAML object")
    return validate_deployment_profile(document, source=source, distribution_root=distribution_root)


def _preset_defaults(preset: Preset) -> StageCapabilities:
    enabled = preset == Preset.FULL
    return StageCapabilities(analytics=enabled, governance=enabled)


def resolve_topology(profile: DeploymentProfile) -> DeploymentTopology:
    """Resolve a `DeploymentProfile` into one effective `DeploymentTopology`.

    A missing stage is disabled -- PROD is never created implicitly. An
    enabled stage without explicit `capabilities` takes the preset default;
    an explicit value always wins over the preset. A disabled stage always
    resolves both capabilities to `False`, regardless of what was written."""
    defaults = _preset_defaults(profile.preset)
    resolved: list[ResolvedStage] = []
    for stage_name in StageName:
        stage = profile.stage(stage_name)
        if stage is None or not stage.enabled:
            resolved.append(ResolvedStage(name=stage_name, enabled=False, capabilities=StageCapabilities()))
            continue
        capabilities = stage.capabilities if stage.capabilities is not None else defaults
        resolved.append(ResolvedStage(name=stage_name, enabled=True, capabilities=capabilities))

    return DeploymentTopology(
        profile_name=profile.name,
        provider=profile.provider.type,
        region=profile.provider.region,
        preset=profile.preset,
        stages=tuple(resolved),
        access=profile.access,
        identity=profile.identity,
        project=profile.project,
        query_size=profile.query_size,
        storage_size=profile.storage_size,
    )


def resolve_project(profile: DeploymentProfile, environ: Mapping[str, str] | None = None) -> ResolvedProject:
    return resolve_declared_project(profile.project, profile.provider.type, environ)


def resolve_declared_project(
    project: ProjectSpec | None, provider: Provider, environ: Mapping[str, str] | None = None
) -> ResolvedProject:
    """Where the project-code image comes from, in precedence order: the profile's
    `spec.project.image`, then `PROJECT_CODE_IMAGE_REPOSITORY` (or its
    `<PROVIDER>_` alias), then the provider registry for a project declared
    without an image, and the public example only when nothing was declared. A
    declared project therefore never resolves to the example."""
    repository = project.repository if project else None
    if project and project.image:
        return ResolvedProject("profile", repository, project.image.repository, project.image.pull_secret_name)
    prefix = provider.value.upper()
    env = (environ or {}).get("PROJECT_CODE_IMAGE_REPOSITORY") or (environ or {}).get(
        f"{prefix}_PROJECT_CODE_IMAGE_REPOSITORY"
    )
    if env:
        return ResolvedProject("environment", repository, _image_repository(env, where="PROJECT_CODE_IMAGE_REPOSITORY"))
    if project:
        return ResolvedProject("provider-default", repository, None)
    return ResolvedProject("public-example", None, PUBLIC_EXAMPLE_IMAGE_REPOSITORY)


def legacy_single_stage_topology(*, provider: Provider, preset: Preset) -> DeploymentTopology:
    """The v0.2 compatibility path: `olf infra deploy --provider <provider>
    --profile <preset>` resolves to one enabled DEV stage using the preset's
    capability defaults. `DeploymentContext` resolves every run through this
    model, so the deprecated shorthand is literally the single-DEV-stage case
    rather than a second code path."""
    profile = DeploymentProfile(
        name="legacy",
        provider=ProviderSpec(type=provider),
        preset=preset,
        stages=(StageSpec(name=StageName.DEV),),
    )
    return resolve_topology(profile)
