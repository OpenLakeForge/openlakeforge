from __future__ import annotations

from pathlib import Path

from openlakeforge_runtime.bronze_csv import BronzeLoadResult, load_entities_to_bronze

CRM_ENTITIES = (
    "accounts",
    "channels",
    "nps_responses",
    "order_lines",
    "orders",
    "products",
    "promotions",
    "subscriptions",
    "support_tickets",
)

_SOURCE_DIR = Path(__file__).resolve().parents[1]
_RAW_DIR = _SOURCE_DIR / "examples"
_BRONZE_PREFIX = "crm"


def load_crm_entities_to_bronze(
    entities: tuple[str, ...],
    raw_dir: Path | None = None,
) -> dict[str, BronzeLoadResult]:
    """Load a subset of CRM resources into Bronze under the ``crm`` prefix."""
    return load_entities_to_bronze(
        entities=entities,
        raw_dir=raw_dir or _RAW_DIR,
        bronze_prefix=_BRONZE_PREFIX,
    )


def load_all_entities_to_bronze(raw_dir: Path | None = None) -> dict[str, BronzeLoadResult]:
    """Load every CRM resource declared in ``bronze/crm/source.yaml``."""
    return load_crm_entities_to_bronze(CRM_ENTITIES, raw_dir=raw_dir)