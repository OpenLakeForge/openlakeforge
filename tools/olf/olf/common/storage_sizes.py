"""The per-size volume table behind `spec.storage.size` (`release/storage-sizes.yaml`).

The Terraform SeaweedFS module reads the same file to render the Helm values, so
this loader is for what `olf` checks against the cluster, not for rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from olf.common.profile import Size

STORAGE_SIZES_PATH = "release/storage-sizes.yaml"

_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL = {"": 1, "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18}
_QUANTITY = re.compile(r"^(?P<number>\d+)(?P<suffix>Ki|Mi|Gi|Ti|Pi|Ei|[kKMGTPE]?)$")


def quantity_bytes(quantity: str) -> int:
    """A Kubernetes storage quantity in bytes. Fractions and milli-units are not storage sizes anyone writes."""
    match = _QUANTITY.match(quantity.strip())
    if match is None:
        raise ValueError(f"not a whole-number Kubernetes storage quantity: {quantity!r}")
    suffix = match["suffix"]
    return int(match["number"]) * (_BINARY.get(suffix) or _DECIMAL[suffix])


@dataclass(frozen=True)
class ObjectStoreVolumes:
    """PVC requests of the SeaweedFS master, filer and volume server for one size."""

    master: str
    filer: str
    volume: str
    max_volumes: int


def load_object_store_volumes(distribution_root: Path) -> dict[Size, ObjectStoreVolumes]:
    path = distribution_root / STORAGE_SIZES_PATH
    document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return {
            Size(name): ObjectStoreVolumes(
                master=str(row["object_store"]["master"]),
                filer=str(row["object_store"]["filer"]),
                volume=str(row["object_store"]["volume"]),
                max_volumes=int(row["object_store"]["max_volumes"]),
            )
            for name, row in document["sizes"].items()
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{STORAGE_SIZES_PATH} is malformed: {exc!r}") from exc
