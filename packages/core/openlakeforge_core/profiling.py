"""Which Gold tables OpenMetadata may profile, and which statistics it may store.

Profiling is opt-in per product (`profiling.enabled` in `governance.yml`) and gated by the product's resolved
classification. The `metrics` list sent to the profiler is what limits storage: the 1.13.6 profiler stores only the
metrics it is asked for, so a classification that withholds `min`, `max` and `mean` simply leaves them out of the list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .governance import resolve_product
from .inventory import Product

__all__ = [
    "PROFILING_CRON",
    "PROFILING_PERIOD",
    "PROFILING_STALE_SLACK",
    "PROFILING_METRICS_COUNTS",
    "PROFILING_METRICS_VALUES",
    "PROFILING_ROLES",
    "ProfilingTarget",
    "profiling_conflicts",
    "profile_is_stale",
    "profiling_metrics",
    "resolve_profiling",
]

PROFILING_ROLES = ("Fact", "Dimension")
PROFILING_METRICS_COUNTS = ("rowCount", "valuesCount", "nullCount", "nullProportion", "distinctCount")
# `min` and `max` are real column values for numeric and date columns (lengths for varchar), so only the
# classifications that allow values may request them.
PROFILING_METRICS_VALUES = ("min", "max", "mean")
# The daily refresh, UTC. Product cadences end by 03:00 (lakehouse_code/pipelines/products.py), so the profile reads
# the day's tables; the slack covers a run that starts late or a crawl that has to precede it.
PROFILING_CRON = "30 4 * * *"
PROFILING_PERIOD = timedelta(hours=24)
PROFILING_STALE_SLACK = timedelta(hours=6)
_VALUE_CLASSIFICATIONS = ("Public", "Internal")
_COUNT_CLASSIFICATIONS = ("Confidential",)


@dataclass(frozen=True)
class ProfilingTarget:
    """One product's profiling scope: its Gold schema, the tables to profile and the metrics to store."""

    product: str
    schema: str
    tables: tuple[str, ...]
    metrics: tuple[str, ...]


def profiling_metrics(classification: str | None) -> tuple[str, ...] | None:
    """The metric list a classification allows, or None when it allows no profiling (Restricted, unclassified)."""
    if classification in _VALUE_CLASSIFICATIONS:
        return (*PROFILING_METRICS_COUNTS, *PROFILING_METRICS_VALUES)
    if classification in _COUNT_CLASSIFICATIONS:
        return PROFILING_METRICS_COUNTS
    return None


def profiling_conflicts(document: Mapping[str, Any]) -> list[str]:
    """Products of a validated governance document that enable profiling on a classification that forbids it."""
    conflicts = []
    for domain in document.get("domains", ()):
        for product in domain.get("products", ()):
            resolved = resolve_product(domain, product)
            enabled = resolved.get("profiling", {}).get("enabled", False)
            if enabled and profiling_metrics(resolved.get("classification")) is None:
                conflicts.append(
                    f"product {resolved['id']!r} enables profiling but its classification is "
                    f"{resolved.get('classification') or 'unset'}; set it to Public, Internal or Confidential, "
                    "or set profiling.enabled to false, or remove profiling"
                )
    return conflicts


def resolve_profiling(
    products: Iterable[Product], roles: Mapping[str, Mapping[str, str]]
) -> tuple[ProfilingTarget, ...]:
    """The profiling scope of each product that enables it and may be profiled, ordered by product id.

    `roles` is `{product: {model: role}}` as `scan_gold_roles` returns it. A product with profiling off, a
    classification that forbids it, or no fact or dimension table has no target: nothing is profiled for it.
    """
    targets = []
    for product in sorted(products, key=lambda p: p.id):
        metrics = profiling_metrics(product.classification)
        if not product.profiling_enabled or metrics is None:
            continue
        tables = tuple(sorted(m for m, role in roles.get(product.id, {}).items() if role in PROFILING_ROLES))
        if tables:
            targets.append(ProfilingTarget(product.id, product.gold_namespace, tables, metrics))
    return tuple(targets)


def profile_is_stale(timestamp_ms: int | float | None, *, now: datetime | None = None) -> bool:
    """A profile is stale when it is absent or older than the refresh period plus its slack.

    A failed or overdue profiler run leaves the last profile in place, so age is the only signal a reader has:
    statistics past this age must not be presented as current.
    """
    if timestamp_ms is None:
        return True
    taken = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return (now or datetime.now(UTC)) - taken > PROFILING_PERIOD + PROFILING_STALE_SLACK
