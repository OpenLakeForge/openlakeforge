"""The OLF ownership marker and the JSON Patch the reconciler writes with.

Every entity OLF creates or manages carries one custom property, `olfManaged`,
whose value is `git:<revision>`: the Git revision whose deploy last stamped
it. A property rather than a tag because a tag cannot carry the revision, and
one tag per revision would grow without bound. The marker is the only thing
that tells OLF's entities from ones a person or the Trino crawler created, so
the drift report reads it to find `stale` entities.

`patch_operations` is the only way the reconciler changes an existing entity:
it touches the fields the caller names and leaves everything else (crawled
columns, human tags, comments, followers) alone.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

MARKER_PROPERTY = "olfManaged"
MARKER_DESCRIPTION = "Set by OpenLakeForge: the Git revision that last reconciled this entity from the project."
# OpenMetadata keys custom properties by entity type name, not collection.
MARKED_ENTITY_TYPES = ("domain", "dataProduct", "databaseSchema", "table", "container", "glossary", "glossaryTerm")
_MARKER_PREFIX = "git:"
_MERGED_FIELDS = ("extension", "sla")


def marker_value(revision: str) -> str:
    return f"{_MARKER_PREFIX}{revision or 'unknown'}"


def marker_of(entity: Mapping[str, Any]) -> str | None:
    """The marker an entity carries, or None when OLF does not own it."""
    value = (entity.get("extension") or {}).get(MARKER_PROPERTY)
    return value if isinstance(value, str) and value.startswith(_MARKER_PREFIX) else None


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _same(current: Any, wanted: Any) -> bool:
    # OpenMetadata 1.13.6 returns text HTML-escaped (a stored `run's` reads back `run&#39;s`), so an owned
    # description with a quote would otherwise be patched on every deploy.
    if isinstance(current, str) and isinstance(wanted, str):
        return current == wanted or html.unescape(current) == wanted
    # A reference list is the same set of entities whatever display fields the server adds to each reference.
    if isinstance(wanted, list) and wanted and all(isinstance(item, Mapping) and "id" in item for item in wanted):
        return isinstance(current, list) and {item.get("id") for item in current} == {item["id"] for item in wanted}
    return bool(current == wanted)


def patch_operations(current: Mapping[str, Any], desired: Mapping[str, Any]) -> list[dict[str, Any]]:
    """RFC 6902 operations that bring `current` to `desired` on the fields `desired` names; none when equal.

    `extension` and `sla` are merged key by key, so a custom property or an SLA field a person set (the SLA also
    holds `availability` and `responseTime`, which OLF does not declare) survives. An empty wanted value on an
    absent field is no difference: OpenMetadata omits empty fields from what it returns, so writing one would
    repeat on every deploy.
    """
    operations: list[dict[str, Any]] = []
    for field, wanted in desired.items():
        have = current.get(field)
        if field in _MERGED_FIELDS and isinstance(have, Mapping):
            for key, value in wanted.items():
                if key not in have:
                    operations.append({"op": "add", "path": f"/{field}/{key}", "value": value})
                elif have[key] != value:
                    operations.append({"op": "replace", "path": f"/{field}/{key}", "value": value})
        elif _is_empty(have):
            if not _is_empty(wanted):
                operations.append({"op": "add", "path": f"/{field}", "value": wanted})
        elif not _same(have, wanted):
            operations.append({"op": "replace", "path": f"/{field}", "value": wanted})
    return operations


def tag_ref(tag_fqn: str) -> dict[str, str]:
    return {"tagFQN": tag_fqn, "source": "Classification", "labelType": "Manual", "state": "Confirmed"}


def tag_operations(current: Mapping[str, Any], wanted: Sequence[str]) -> list[dict[str, Any]]:
    """JSON Patch that gives an entity the `wanted` tag FQNs and touches no other tag.

    A tag is only removed when it belongs to the same classification as a wanted one: Tier and the
    DataSensitivity classification are mutually exclusive, so a second value would be refused. A tag a person
    added from another classification stays, and so does a derived one, which is not ours to remove.
    """
    if not wanted:
        return []
    have = current.get("tags")
    # A glossary term has the same `<name>.<term>` shape; only classification labels are ours to compare or remove.
    # Indices are positions in the stored list, which is what a JSON Patch path names.
    held = [
        (index, tag)
        for index, tag in enumerate(have or [])
        if isinstance(tag, Mapping) and tag.get("source", "Classification") == "Classification"
    ]
    classifications = {fqn.partition(".")[0] for fqn in wanted}
    operations: list[dict[str, Any]] = [
        {"op": "remove", "path": f"/tags/{index}"}
        for index, tag in reversed(held)
        if tag["tagFQN"].partition(".")[0] in classifications
        and tag["tagFQN"] not in wanted
        and tag.get("labelType") != "Derived"
    ]
    present = {tag["tagFQN"] for _, tag in held}
    missing = [tag_ref(fqn) for fqn in wanted if fqn not in present]
    if not missing:
        return operations
    if have is None:
        return [{"op": "add", "path": "/tags", "value": missing}]
    return operations + [{"op": "add", "path": "/tags/-", "value": tag} for tag in missing]
