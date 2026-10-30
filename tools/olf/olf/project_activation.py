"""Immutable, stage-bound activation records for ProjectRevisions (#115)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from olf.artifact_store import ArtifactStoreError, RevisionStore, publish_immutable, read_required
from olf.profile import StageName

ACTIVATION_API_VERSION = "openlakeforge.io/v1alpha1"
ACTIVATION_KIND = "ProjectActivation"
POINTER_KIND = "ProjectActivationPointer"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProjectActivationError(RuntimeError):
    """An activation record is malformed, unavailable, or unsafe to change."""


def _digest(value: object, *, field: str) -> str:
    # `object`, not `str`: every caller passes a value straight out of parsed
    # JSON, and rejecting a non-string is this function's job. Declaring `str`
    # claimed the callers had already done the narrowing they were calling
    # here to get.
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ProjectActivationError(f"{field} must be sha256:<64 lowercase hex characters>.")
    return value


def _canonical(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ProjectActivation:
    """The complete executable binding of a revision to exactly one stage."""

    deployment_profile: str
    provider: str
    stage: StageName
    project_name: str
    project_revision: str
    distribution_version: str
    project_code_image: str
    provider_binding_digest: str
    capabilities: dict[str, bool]
    activation_revision: str = ""
    # Schema 1 only. Floe manifests were rendered and published per stage
    # until Floe 0.8 let the code location build them in memory; a record
    # that carries one is still read, and still hashes, exactly as written,
    # so a stage activated before the change can be inspected and rolled
    # back onto.
    floe_manifest_revision: str = ""

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "apiVersion": ACTIVATION_API_VERSION,
            "kind": ACTIVATION_KIND,
            "schema_version": 1 if self.floe_manifest_revision else 2,
            "deployment_profile": self.deployment_profile,
            "provider": self.provider,
            "stage": StageName(self.stage).value,
            "project_name": self.project_name,
            "project_revision": self.project_revision,
            "distribution_version": self.distribution_version,
            "project_code_image": self.project_code_image,
            "provider_binding_digest": self.provider_binding_digest,
            "capabilities": dict(sorted(self.capabilities.items())),
        }
        if self.floe_manifest_revision:
            payload["floe_manifest_revision"] = self.floe_manifest_revision
        return payload

    def resolved(self) -> ProjectActivation:
        self.validate(allow_unresolved=True)
        revision = "sha256:" + hashlib.sha256(_canonical(self._payload())).hexdigest()
        if self.activation_revision and self.activation_revision != revision:
            raise ProjectActivationError(
                f"activation_revision {self.activation_revision} does not match the canonical activation content."
            )
        return ProjectActivation(**{**self.__dict__, "stage": StageName(self.stage), "activation_revision": revision})

    def matches_inputs(self, other: ProjectActivation) -> bool:
        """Compare every activation input, ignoring a schema-1 Floe manifest revision.

        A schema-1 record also binds the Floe manifests its stage rendered,
        which no schema-2 activation has, so comparing whole records would
        never find an earlier activation equal to the inputs being applied.
        """
        fields = (
            "deployment_profile",
            "provider",
            "project_name",
            "project_revision",
            "distribution_version",
            "project_code_image",
            "provider_binding_digest",
        )
        return (
            all(getattr(self, name) == getattr(other, name) for name in fields)
            and StageName(self.stage) == StageName(other.stage)
            and dict(self.capabilities) == dict(other.capabilities)
        )

    def validate(self, *, allow_unresolved: bool = False) -> None:
        if not self.deployment_profile or not self.project_name or not self.distribution_version:
            raise ProjectActivationError(
                "activation deployment_profile, project_name, and distribution_version are required."
            )
        if self.provider not in {"local", "aws", "azure"}:
            raise ProjectActivationError(f"unsupported activation provider {self.provider!r}.")
        try:
            StageName(self.stage)
        except ValueError as exc:
            raise ProjectActivationError(f"unsupported activation stage {self.stage!r}.") from exc
        _digest(self.project_revision, field="project_revision")
        if self.floe_manifest_revision:
            _digest(self.floe_manifest_revision, field="floe_manifest_revision")
        _digest(self.provider_binding_digest, field="provider_binding_digest")
        if "@sha256:" not in self.project_code_image:
            raise ProjectActivationError("project_code_image must be digest-pinned.")
        if set(self.capabilities) - {"analytics", "governance"}:
            raise ProjectActivationError("activation capabilities may only declare analytics and governance.")
        if not all(isinstance(value, bool) for value in self.capabilities.values()):
            raise ProjectActivationError("activation capability outcomes must be booleans.")
        if not allow_unresolved:
            _digest(self.activation_revision, field="activation_revision")

    def to_json(self) -> str:
        resolved = self.resolved()
        payload = {**resolved._payload(), "activation_revision": resolved.activation_revision}
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, value: str) -> ProjectActivation:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProjectActivationError(f"malformed ACTIVATION.json: {exc}") from exc
        required = {
            "apiVersion",
            "kind",
            "schema_version",
            "deployment_profile",
            "provider",
            "stage",
            "project_name",
            "project_revision",
            "distribution_version",
            "project_code_image",
            "provider_binding_digest",
            "capabilities",
            "activation_revision",
        }
        schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
        if schema_version == 1:
            required.add("floe_manifest_revision")
        if not isinstance(payload, dict) or set(payload) != required:
            raise ProjectActivationError("ACTIVATION.json has an unsupported field set.")
        if (
            payload["apiVersion"] != ACTIVATION_API_VERSION
            or payload["kind"] != ACTIVATION_KIND
            or schema_version not in (1, 2)
        ):
            raise ProjectActivationError("ACTIVATION.json has an unsupported apiVersion, kind, or schema_version.")
        try:
            activation = cls(
                deployment_profile=payload["deployment_profile"],
                provider=payload["provider"],
                stage=StageName(payload["stage"]),
                project_name=payload["project_name"],
                project_revision=payload["project_revision"],
                distribution_version=payload["distribution_version"],
                project_code_image=payload["project_code_image"],
                floe_manifest_revision=payload.get("floe_manifest_revision", ""),
                provider_binding_digest=payload["provider_binding_digest"],
                capabilities=payload["capabilities"],
                activation_revision=payload["activation_revision"],
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ProjectActivationError(f"malformed ACTIVATION.json: {exc}") from exc
        activation.validate()
        return activation.resolved()


def activation_key(activation_revision: str, *, stage: StageName | str) -> str:
    digest = _digest(activation_revision, field="activation_revision").removeprefix("sha256:")
    return f"activations/{StageName(stage).value}/revisions/sha256/{digest}/ACTIVATION.json"


def active_key(stage: StageName | str) -> str:
    return f"activations/{StageName(stage).value}/ACTIVE.json"


def publish(store: RevisionStore, activation: ProjectActivation) -> ProjectActivation:
    resolved = activation.resolved()
    try:
        publish_immutable(
            store,
            activation_key(resolved.activation_revision, stage=resolved.stage),
            resolved.to_json().encode(),
            content_type="application/json",
        )
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc
    return resolved


def read(store: RevisionStore, activation_revision: str, *, stage: StageName | str) -> ProjectActivation:
    try:
        activation = ProjectActivation.from_json(
            read_required(store, activation_key(activation_revision, stage=stage)).decode()
        )
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc
    if activation.activation_revision != activation_revision or activation.stage != StageName(stage):
        raise ProjectActivationError("activation manifest does not match its requested stage or revision.")
    return activation


def commit_active(store: RevisionStore, activation: ProjectActivation) -> None:
    resolved = activation.resolved()
    pointer = _canonical(
        {
            "apiVersion": ACTIVATION_API_VERSION,
            "kind": POINTER_KIND,
            "schema_version": 1,
            "activation_revision": resolved.activation_revision,
        }
    )
    try:
        store.write(active_key(resolved.stage), pointer, content_type="application/json")
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc


def active(store: RevisionStore, *, stage: StageName | str) -> ProjectActivation | None:
    try:
        content = store.read(active_key(stage))
    except ArtifactStoreError as exc:
        raise ProjectActivationError(str(exc)) from exc
    if content is None:
        return None
    try:
        pointer = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProjectActivationError(f"malformed ACTIVE.json: {exc}") from exc
    expected = {"apiVersion", "kind", "schema_version", "activation_revision"}
    if (
        not isinstance(pointer, dict)
        or set(pointer) != expected
        or pointer.get("apiVersion") != ACTIVATION_API_VERSION
        or pointer.get("kind") != POINTER_KIND
        or pointer.get("schema_version") != 1
    ):
        raise ProjectActivationError("ACTIVE.json has an unsupported field set.")
    return read(
        store, _digest(pointer.get("activation_revision"), field="ACTIVE.json activation_revision"), stage=stage
    )
