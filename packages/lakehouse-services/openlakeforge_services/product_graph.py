"""Which data products a product consumes from, as OpenMetadata's `DataProduct.consumesFrom`.

Git declares the edges (the cross-product `ref()` graph, `openlakeforge_core.product_dependencies`). A deploy only
adds a missing edge: an edge OpenMetadata holds and Git no longer declares is reported by the drift report for a person
to remove, never deleted (ownership guide). `providesTo` is not written: OpenMetadata 1.13.6 did not return it after a
`consumesFrom` edit (docs/testing/openmetadata-governance-findings.md), so it is neither relied on nor read.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .openmetadata import OpenMetadataError

if TYPE_CHECKING:
    from .governance import MetadataReconciler

FIELDS = "consumesFrom,extension"


def producer_ids(reconciler: MetadataReconciler, producers: Collection[str]) -> dict[str, str | None]:
    """Each producer's data product id, None when OpenMetadata does not hold it."""
    return {name: (reconciler.lookup("dataProducts", name, "") or {}).get("id") for name in sorted(producers)}


def consumes_from_operations(current: Mapping[str, Any], wanted_ids: Collection[str]) -> list[dict[str, Any]]:
    """JSON Patch that adds the `wanted_ids` the product does not consume from yet, and removes nothing.

    The whole list is written, as the findings probe verified, with the edges already held kept in front of it
    (an edge a person added survives). Appending with `/consumesFrom/-` was not probed on 1.13.6.
    """
    have = [{"id": ref["id"], "type": "dataProduct"} for ref in current.get("consumesFrom") or []]
    held = {ref["id"] for ref in have}
    missing = [{"id": i, "type": "dataProduct"} for i in sorted(wanted_ids) if i not in held]
    return [{"op": "add", "path": "/consumesFrom", "value": have + missing}] if missing else []


def reconcile_consumes_from(reconciler: MetadataReconciler, product: str, producers: Sequence[str]) -> int:
    """Add the edges from `product` to each of `producers` it lacks; returns how many were added.

    Every producer is reconciled before this runs, so one OpenMetadata cannot find is an error, not a skip.
    """
    ids = producer_ids(reconciler, producers)
    unknown = sorted(name for name, found in ids.items() if not found)
    if unknown:
        raise OpenMetadataError(f"data product {product!r} consumes from {unknown}, which OpenMetadata does not hold.")
    wanted = [i for i in ids.values() if i]
    current = reconciler.lookup("dataProducts", product, FIELDS)
    if current is None:
        raise OpenMetadataError(f"OpenMetadata does not hold data product {product!r}.")
    operations = consumes_from_operations(current, wanted)
    if not operations:
        return 0
    reconciler.client.request(
        "PATCH",
        f"/api/v1/dataProducts/{current['id']}",
        json_body=operations,
        content_type="application/json-patch+json",
    )
    # A 200 alone is not taken as success: read the edges back, as `MetadataReconciler.reconcile` does.
    stored = reconciler.lookup("dataProducts", product, FIELDS) or {}
    if consumes_from_operations(stored, wanted):
        raise OpenMetadataError(
            f"OpenMetadata accepted the update of data product {product!r} but reads back without its consumesFrom "
            "edges; the server ignored it."
        )
    return len(operations[0]["value"]) - len(current.get("consumesFrom") or [])


def consumes_from_drift(reconciler: MetadataReconciler, product: str, producers: Sequence[str]) -> list[str]:
    """What differs between Git's edges for `product` and the ones OpenMetadata holds. Reads only."""
    current = reconciler.lookup("dataProducts", product, FIELDS)
    if current is None:
        return []  # the data product itself is reported missing
    ids = producer_ids(reconciler, producers)
    details = [f"consumesFrom: {name} not found" for name, found in ids.items() if not found]
    wanted = {found for found in ids.values() if found}
    details += [
        f"consumesFrom: {name} not attached"
        for name, found in ids.items()
        if found and consumes_from_operations(current, [found])
    ]
    for ref in current.get("consumesFrom") or []:
        if ref["id"] not in wanted:
            label = ref.get("fullyQualifiedName") or ref.get("name") or ref["id"]
            details.append(f"consumesFrom: {label} is held but no longer declared")
    return details
