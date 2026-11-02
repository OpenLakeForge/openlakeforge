from __future__ import annotations

from pathlib import Path

from openlakeforge_runtime.bronze_csv import BronzeLoadResult, load_entities_to_bronze

ERP_ENTITIES = (
    "warehouses",
    "suppliers",
    "inventory_snapshots",
    "purchase_orders",
    "shipments",
    "stockout_events",
)

_SOURCE_DIR = Path(__file__).resolve().parents[1]
_RAW_DIR = _SOURCE_DIR / "examples"
_BRONZE_PREFIX = "erp"


def load_erp_entities_to_bronze(
    entities: tuple[str, ...],
    raw_dir: Path | None = None,
) -> dict[str, BronzeLoadResult]:
    """Load a subset of ERP resources into Bronze under the ``erp`` prefix."""
    return load_entities_to_bronze(
        entities=entities,
        raw_dir=raw_dir or _RAW_DIR,
        bronze_prefix=_BRONZE_PREFIX,
    )


def load_all_entities_to_bronze(raw_dir: Path | None = None) -> dict[str, BronzeLoadResult]:
    """Load every ERP resource declared in ``bronze/erp/source.yaml``."""
    return load_erp_entities_to_bronze(ERP_ENTITIES, raw_dir=raw_dir)