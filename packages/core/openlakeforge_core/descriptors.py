"""Versioned descriptor validation for the Source kind and the shared field checks.

The canonical model is the ``Governance`` descriptor at
``lakehouse_code/governance.yml`` (``governance.py``) together with one ``Source``
descriptor per bronze source at ``lakehouse_code/bronze/<source>/source.yaml``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

SOURCE_API_VERSION = "openlakeforge.io/v1alpha3"
SOURCE_KIND = "Source"
_PRODUCT_IDENTIFIER_PATTERN = r"[a-z][a-z0-9_]*"
_IDENTIFIER_PATTERN = re.compile(rf"^{_PRODUCT_IDENTIFIER_PATTERN}$")
# Underscore-separated words, no `_` at either end or doubled. A source that
# declares runtime keys turns its name into Kubernetes object names (`_` -> `-`,
# which may not end a name) and both into dlt variables (`__` separates dlt
# config sections), so `crm_` or `a__b` cannot be bound unambiguously.
_RUNTIME_WORDS_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class LakehouseDescriptorError(ValueError):
    """Raised when a v1alpha3 lakehouse or source descriptor is invalid."""


def _identifier(value: object, *, field: str, source: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise LakehouseDescriptorError(f"{source}: {field} must match '^[a-z][a-z0-9_]*$'")
    return value


def _non_empty_string(value: object, *, field: str, source: str) -> str:
    if not isinstance(value, str) or not value:
        raise LakehouseDescriptorError(f"{source}: {field} must be a non-empty string")
    return value


def _optional_string(value: object, *, field: str, source: str) -> None:
    if value is not None and not isinstance(value, str):
        raise LakehouseDescriptorError(f"{source}: {field} must be a string")


def validate_source_descriptor(document: Mapping[str, Any], *, source: str = "source.yaml") -> None:
    """Validate a v1alpha3 Source descriptor envelope and resource list."""
    if document.get("apiVersion") != SOURCE_API_VERSION:
        raise LakehouseDescriptorError(
            f"{source}: unsupported apiVersion {document.get('apiVersion')!r}; expected {SOURCE_API_VERSION!r}"
        )
    if document.get("kind") != SOURCE_KIND:
        raise LakehouseDescriptorError(f"{source}: kind must be {SOURCE_KIND!r}")
    for field in ("name", "displayName", "description", "status", "resources"):
        if field not in document:
            raise LakehouseDescriptorError(f"{source}: missing required field {field!r}")
    _identifier(document["name"], field="name", source=source)
    _non_empty_string(document["displayName"], field="displayName", source=source)
    _non_empty_string(document["status"], field="status", source=source)
    _optional_string(document["description"], field="description", source=source)
    resources = document["resources"]
    if not isinstance(resources, list) or not resources:
        raise LakehouseDescriptorError(f"{source}: resources must be a non-empty array")
    seen: set[str] = set()
    for index, resource in enumerate(resources):
        if not isinstance(resource, Mapping):
            raise LakehouseDescriptorError(f"{source}: resources[{index}] must be an object")
        if "name" not in resource:
            raise LakehouseDescriptorError(f"{source}: resources[{index}] is missing required field 'name'")
        name = _identifier(resource["name"], field=f"resources[{index}].name", source=source)
        if name in seen:
            raise LakehouseDescriptorError(f"{source}: duplicate resource name {name!r}")
        seen.add(name)
        if "description" in resource and not isinstance(resource["description"], str):
            raise LakehouseDescriptorError(f"{source}: resources[{index}].description must be a string")
        if "load" in resource:
            _validate_resource_load(resource["load"], field=f"resources[{index}].load", source=source)
        unexpected = set(resource) - {"name", "description", "load"}
        if unexpected:
            raise LakehouseDescriptorError(
                f"{source}: resources[{index}] must not contain {sorted(unexpected)!r} (provider-neutral fields only)"
            )
    if "runtime" in document:
        if not _RUNTIME_WORDS_PATTERN.fullmatch(document["name"]):
            raise LakehouseDescriptorError(
                f"{source}: a source with runtime keys needs a name of underscore-separated words "
                f"(no leading, trailing or doubled '_'); got {document['name']!r}"
            )
        _validate_source_runtime(document["runtime"], source=source)
    unexpected = set(document) - {
        "apiVersion",
        "kind",
        "name",
        "displayName",
        "description",
        "status",
        "resources",
        "runtime",
    }
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: must not contain {sorted(unexpected)!r} (provider-neutral fields only)"
        )


LOAD_MODES = ("full", "incremental")


def _validate_resource_load(load: object, *, field: str, source: str) -> None:
    # Column names, not values or queries: the connector owns how it reads the
    # cursor, the descriptor owns which column it is and what Silver merges on.
    if not isinstance(load, Mapping):
        raise LakehouseDescriptorError(f"{source}: {field} must be an object")
    unexpected = set(load) - {"mode", "cursor", "primary_key"}
    if unexpected:
        raise LakehouseDescriptorError(f"{source}: {field} must not contain {sorted(unexpected)!r}")
    mode = load.get("mode")
    if mode not in LOAD_MODES:
        raise LakehouseDescriptorError(f"{source}: {field}.mode must be one of {list(LOAD_MODES)!r}, got {mode!r}")
    if "cursor" in load:
        _identifier(load["cursor"], field=f"{field}.cursor", source=source)
    if "primary_key" in load:
        keys = load["primary_key"]
        if not isinstance(keys, list) or not keys:
            raise LakehouseDescriptorError(f"{source}: {field}.primary_key must be a non-empty array of column names")
        for index, key in enumerate(keys):
            _identifier(key, field=f"{field}.primary_key[{index}]", source=source)
        if len(set(keys)) != len(keys):
            raise LakehouseDescriptorError(f"{source}: {field}.primary_key lists a column twice")
    if mode == "incremental":
        # Without a cursor every run is a full extract appended to the last
        # one; without a key Silver cannot merge a changed row onto its old
        # version.
        missing = [key for key in ("cursor", "primary_key") if key not in load]
        if missing:
            raise LakehouseDescriptorError(f"{source}: {field} is incremental and needs {' and '.join(missing)}")
    elif "cursor" in load:
        raise LakehouseDescriptorError(f"{source}: {field}.cursor applies to mode 'incremental' only")


def _validate_source_runtime(runtime: object, *, source: str) -> None:
    # Key names only: their values are a stage's to bind, in its own ConfigMap
    # and Secret, so a revision promotes unchanged from DEV to PROD (#546).
    if not isinstance(runtime, Mapping):
        raise LakehouseDescriptorError(f"{source}: runtime must be an object")
    unexpected = set(runtime) - {"config", "secrets"}
    if unexpected:
        raise LakehouseDescriptorError(
            f"{source}: runtime must not contain {sorted(unexpected)!r}; it names config and secrets keys only"
        )
    listed: dict[str, str] = {}
    for group in ("config", "secrets"):
        keys = runtime.get(group, [])
        if not isinstance(keys, list):
            raise LakehouseDescriptorError(f"{source}: runtime.{group} must be an array of key names")
        for index, key in enumerate(keys):
            name = _identifier(key, field=f"runtime.{group}[{index}]", source=source)
            if not _RUNTIME_WORDS_PATTERN.fullmatch(name):
                raise LakehouseDescriptorError(
                    f"{source}: runtime.{group}[{index}] must be underscore-separated words "
                    f"(no leading, trailing or doubled '_'); got {name!r}"
                )
            if name in listed:
                raise LakehouseDescriptorError(
                    f"{source}: runtime key {name!r} is listed twice (runtime.{listed[name]} and runtime.{group})"
                )
            listed[name] = group


def load_source_descriptor(path: str | Path) -> dict[str, Any]:
    """Load and validate the v1alpha3 Source descriptor at ``path``."""
    source = str(path)
    with Path(path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise LakehouseDescriptorError(f"{source}: descriptor must contain a YAML object")
    validate_source_descriptor(document, source=source)
    return document

