"""Test that ERP demo CSV fixtures maintain referential integrity.

Every purchase_order_id referenced in shipments.csv must exist in
purchase_orders.csv. Demo supplier delivery metrics join the two fixtures, so
an orphaned shipment would be dropped silently from those metrics.

See: https://github.com/OpenLakeForge/openlakeforge/issues/484
"""

from __future__ import annotations

import csv
from pathlib import Path


def test_erp_demo_shipments_reference_existing_purchase_orders() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    purchase_orders_path = repo_root / "lakehouse_code/bronze/erp/examples/purchase_orders.csv"
    shipments_path = repo_root / "lakehouse_code/bronze/erp/examples/shipments.csv"

    with purchase_orders_path.open(encoding="utf-8") as f:
        valid_purchase_order_ids = {row["purchase_order_id"] for row in csv.DictReader(f)}

    orphaned_shipments: list[tuple[str, str]] = []
    with shipments_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["purchase_order_id"] not in valid_purchase_order_ids:
                orphaned_shipments.append((row["shipment_id"], row["purchase_order_id"]))

    assert not orphaned_shipments, (
        f"Found {len(orphaned_shipments)} shipment(s) referencing missing purchase orders:\n"
        + "\n".join(f"  {shipment_id} -> {purchase_order_id}" for shipment_id, purchase_order_id in orphaned_shipments)
    )
