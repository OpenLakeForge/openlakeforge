"""The read side of a published `ProjectRevision` sidecar (ADR 0012, ADR 0023 decision 10).

Lives in core so the alert evaluator, whose image has no `olf`, can find a revision's files and check their
digests. Building, scanning and verifying a revision stay in `olf.infra.project_revision`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

MANIFEST_API_VERSION = "openlakeforge.io/v1alpha1"
MANIFEST_KIND = "ProjectRevision"
REVISION_PREFIX = "project/revisions"
SIDECAR_NAME = "PROJECT-REVISION.json"
SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (1, 2, SCHEMA_VERSION)


class ProjectRevisionError(RuntimeError):
    """A project revision cannot be built, published, or verified safely."""


@dataclass(frozen=True)
class ComponentEntries:
    """One named component of the project and the content digest of each of its files."""

    name: str
    entries: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "entries": dict(sorted(self.entries.items()))}

    @classmethod
    def from_dict(cls, payload: Any) -> ComponentEntries:
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("name"), str)
            or not isinstance(payload.get("entries"), dict)
            or not all(isinstance(key, str) and isinstance(digest, str) for key, digest in payload["entries"].items())
        ):
            raise ProjectRevisionError(f"malformed component entry: {payload!r}")
        return cls(name=payload["name"], entries=dict(payload["entries"]))


def _revision_digest(revision: str) -> str:
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", revision)
    if not match:
        raise ProjectRevisionError(f"invalid revision {revision!r}; expected sha256:<64 lowercase hex characters>.")
    return match.group(1)


def revision_prefix(revision: str) -> str:
    return f"{REVISION_PREFIX}/sha256/{_revision_digest(revision)}"


def sidecar_key(revision: str) -> str:
    return f"{revision_prefix(revision)}/{SIDECAR_NAME}"


def aggregate_components(components: Iterable[ComponentEntries]) -> str:
    flattened = {
        f"{component.name}/{key}": digest for component in components for key, digest in component.entries.items()
    }
    if not flattened:
        raise ProjectRevisionError("cannot compute a revision for an empty project.")
    hasher = hashlib.sha256()
    for key in sorted(flattened):
        hasher.update(f"{key}\0{flattened[key]}\n".encode())
    return f"sha256:{hasher.hexdigest()}"


def sidecar_payload(value: str) -> dict[str, Any]:
    """The sidecar's JSON object, once its apiVersion, kind and schema version are ones this reader knows."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProjectRevisionError(f"malformed {SIDECAR_NAME}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectRevisionError(f"malformed {SIDECAR_NAME}: expected a JSON object")
    if payload.get("apiVersion") != MANIFEST_API_VERSION or payload.get("kind") != MANIFEST_KIND:
        raise ProjectRevisionError(
            f"unsupported {SIDECAR_NAME} apiVersion/kind: {payload.get('apiVersion')!r}/{payload.get('kind')!r}"
        )
    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ProjectRevisionError(
            f"unsupported {SIDECAR_NAME} schema_version {schema_version!r}; "
            f"this olf reads {', '.join(map(str, SUPPORTED_SCHEMA_VERSIONS))}."
        )
    return payload


def sidecar_components(value: str, revision: str) -> tuple[ComponentEntries, ...]:
    """The components of the sidecar read at `revision`, once they aggregate to that revision.

    That check is what ties each file digest to the revision the activation names: a sidecar edited after
    publication no longer aggregates to its own key.
    """
    payload = sidecar_payload(value)
    raw = payload.get("components")
    if not isinstance(raw, list):
        raise ProjectRevisionError(f"malformed {SIDECAR_NAME}: 'components' must be an array")
    components = tuple(ComponentEntries.from_dict(item) for item in raw)
    declared = payload.get("revision")
    if declared != revision:
        raise ProjectRevisionError(f"sidecar at {sidecar_key(revision)} declares {declared}, not requested {revision}.")
    actual = aggregate_components(components)
    if actual != revision:
        raise ProjectRevisionError(f"revision sidecar declares {revision}, but its components aggregate to {actual}.")
    return components
