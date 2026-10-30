"""`spec.query.size` as Trino capacity, read from `release/query-sizes.yaml`.

The profile names a size and nothing else; this module is the one place a size
becomes workers and memory. `olf` hands the result to every provider root as the
`query_sizing` Terraform variable, so the mapping is the same on kind, EKS and
AKS.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from olf.common.profile import Size

QUERY_SIZES_PATH = "release/query-sizes.yaml"

_NODE_FIELDS = ("heap", "cpu_request", "memory_request", "memory_limit")
_SIZE_FIELDS = {"workers", "schedule_on_coordinator", "max_memory", "max_memory_per_node", "coordinator", "worker"}


class QuerySizingError(RuntimeError):
    """`release/query-sizes.yaml` is missing a size or malformed."""


@dataclass(frozen=True)
class NodeResources:
    heap: str
    cpu_request: str
    memory_request: str
    memory_limit: str


@dataclass(frozen=True)
class QuerySizing:
    size: str
    workers: int
    schedule_on_coordinator: bool
    max_memory: str
    max_memory_per_node: str
    coordinator: NodeResources
    worker: NodeResources | None

    def terraform_value(self) -> dict[str, Any]:
        """The `query_sizing` variable, which modules/query/trino declares field for field."""
        return asdict(self)


def load_query_sizing(distribution_root: Path, size: Size) -> QuerySizing:
    path = distribution_root / QUERY_SIZES_PATH
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise QuerySizingError(f"cannot read {path}: {exc}") from exc
    sizes = document.get("sizes") if isinstance(document, Mapping) else None
    entry = sizes.get(size.value) if isinstance(sizes, Mapping) else None
    if not isinstance(entry, Mapping):
        raise QuerySizingError(f"{path} defines no size {size.value!r}")
    where = f"{path}: sizes.{size.value}"
    unexpected = set(entry) - _SIZE_FIELDS
    if unexpected:
        raise QuerySizingError(f"{where} has unknown field(s) {sorted(unexpected)}")
    workers = entry.get("workers")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 0:
        raise QuerySizingError(f"{where}.workers must be a non-negative integer")
    if (workers > 0) != ("worker" in entry):
        raise QuerySizingError(f"{where}: worker resources are required exactly when workers > 0")
    schedule = entry.get("schedule_on_coordinator")
    if not isinstance(schedule, bool):
        raise QuerySizingError(f"{where}.schedule_on_coordinator must be a boolean")
    if workers == 0 and not schedule:
        raise QuerySizingError(f"{where}: a coordinator-only size must schedule queries on the coordinator")
    return QuerySizing(
        size=size.value,
        workers=workers,
        schedule_on_coordinator=schedule,
        max_memory=_text(entry, "max_memory", where),
        max_memory_per_node=_text(entry, "max_memory_per_node", where),
        coordinator=_node(entry.get("coordinator"), f"{where}.coordinator"),
        worker=_node(entry["worker"], f"{where}.worker") if workers > 0 else None,
    )


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise QuerySizingError(f"{where}.{key} must be a non-empty string")
    return value


def _node(document: object, where: str) -> NodeResources:
    if not isinstance(document, Mapping) or set(document) != set(_NODE_FIELDS):
        raise QuerySizingError(f"{where} must set exactly {list(_NODE_FIELDS)}")
    return NodeResources(**{field: _text(document, field, where) for field in _NODE_FIELDS})
