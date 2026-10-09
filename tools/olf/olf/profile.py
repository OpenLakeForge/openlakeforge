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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from olf import config
from olf.deployment.context import Provider

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
_SPEC_FIELDS = {"provider", "preset", "stages", "access", "identity"}
_PROVIDER_FIELDS = {"type", "region"}
_ACCESS_FIELDS = {"base_domain", "issuer"}
_IDENTITY_FIELDS = {"issuer", "issuer_url", "role_claim", "role_mapping", "client_ids"}
_IDENTITY_ISSUERS = ("keycloak", "external")
# Dot-separated DNS labels; every route host is `<service>[.<stage>].<base_domain>`.
_BASE_DOMAIN_PATTERN = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
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


@dataclass(frozen=True)
class ProviderSpec:
    type: Provider
    region: str | None = None


@dataclass(frozen=True)
class AccessSpec:
    """Where user-facing services are published (ADR 0013). The defaults are
    the local evaluation install: `*.olf.localhost` names and a local CA."""

    base_domain: str = "olf.localhost"
    issuer: str = "local-ca"


@dataclass(frozen=True)
class IdentitySpec:
    """Which OIDC issuer signs people in (ADR 0014 seam 2). `keycloak` is the
    on-prem default OpenLakeForge deploys; `external` points at an existing
    issuer and must say how its claims map to canonical roles. Credentials never
    appear here: client secrets are Secret references in the provider contract."""

    issuer: str = "keycloak"
    issuer_url: str | None = None
    role_claim: str | None = None
    role_mapping: Mapping[str, tuple[str, ...]] | None = None
    # Issuer-assigned client ids (Cognito, Entra), by consumer; absent = the consumer name.
    client_ids: Mapping[str, str] | None = None


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
    access: AccessSpec = AccessSpec()
    identity: IdentitySpec = IdentitySpec()

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
    access: AccessSpec = AccessSpec()
    identity: IdentitySpec = IdentitySpec()

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
                "access": {"base_domain": self.access.base_domain, "issuer": self.access.issuer},
                "identity": vars(self.identity),
            },
            sort_keys=True,
            default=dict,  # IdentitySpec.role_mapping is a read-only MappingProxyType
        )


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


def _validate_access(document: object, *, source: str) -> AccessSpec:
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{source}: spec.access must be an object")
    _forbid_unexpected(document, _ACCESS_FIELDS, where=f"{source}: spec.access")
    defaults = AccessSpec()
    base_domain = document.get("base_domain", defaults.base_domain)
    if not isinstance(base_domain, str) or not _BASE_DOMAIN_PATTERN.fullmatch(base_domain):
        raise DeploymentProfileError(f"{source}: spec.access.base_domain must be a lowercase DNS name with a dot")
    issuer = _identifier(document.get("issuer", defaults.issuer), field="spec.access.issuer", source=source)
    return AccessSpec(base_domain=base_domain, issuer=issuer)


def _validate_identity(document: object, *, source: str, distribution_root: Path | None = None) -> IdentitySpec:
    where = f"{source}: spec.identity"
    if not isinstance(document, Mapping):
        raise DeploymentProfileError(f"{where} must be an object")
    _forbid_unexpected(document, _IDENTITY_FIELDS, where=where)
    issuer = document.get("issuer", "keycloak")
    if issuer not in _IDENTITY_ISSUERS:
        raise DeploymentProfileError(f"{where}.issuer must be one of {list(_IDENTITY_ISSUERS)!r}")
    if issuer == "external":
        missing = sorted({"issuer_url", "role_claim", "role_mapping"} - set(document))
        if missing:
            raise DeploymentProfileError(f"{where}: issuer 'external' requires {missing!r}")
    issuer_url = document.get("issuer_url")
    if "issuer_url" in document:  # by key: an explicit null is invalid, not omitted
        # Lazy: provider_contracts imports this module (cycle).
        from olf.provider_contracts._validation import ProviderContractError, _oidc_issuer_url

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
        # Fail closed on a role the product does not define (release/identity-roles.yaml).
        roles_file = (distribution_root or config.distribution_root()) / "release/identity-roles.yaml"
        try:
            roles = yaml.safe_load(roles_file.read_text("utf-8"))
        except OSError as exc:
            raise DeploymentProfileError(f"{where}.role_mapping: cannot read {roles_file}: {exc.strerror}") from exc
        for role, values in raw.items():
            if role not in roles["precedence"]:
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
            raise DeploymentProfileError(f"{where}.client_ids requires issuer 'external' (keycloak names its clients)")
        consumers = {"perimeter", "superset", "openmetadata", "trino"}
        if not isinstance(client_ids, Mapping) or not client_ids or set(client_ids) - consumers:
            raise DeploymentProfileError(f"{where}.client_ids must be a non-empty object keyed by {sorted(consumers)}")
        if not all(isinstance(v, str) and v for v in client_ids.values()):
            raise DeploymentProfileError(f"{where}.client_ids values must be non-empty strings")
        client_ids = MappingProxyType(dict(client_ids))
    return IdentitySpec(
        issuer=issuer, issuer_url=issuer_url, role_claim=role_claim, role_mapping=mapping, client_ids=client_ids
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

    access = _validate_access(spec.get("access", {}), source=source)
    identity = _validate_identity(spec.get("identity", {}), source=source, distribution_root=distribution_root)
    return DeploymentProfile(
        name=name, provider=provider, preset=preset, stages=stages, access=access, identity=identity
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
    )


def legacy_single_stage_topology(*, provider: Provider, preset: Preset) -> DeploymentTopology:
    """The v0.2 compatibility path: `olf deploy --provider <provider>
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
