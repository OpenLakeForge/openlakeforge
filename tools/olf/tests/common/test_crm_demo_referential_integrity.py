"""Test that CRM demo CSV fixtures maintain referential integrity.

The seeded demonstration data must not contain orphaned order lines — every
order_id referenced in order_lines.csv must exist in orders.csv. If this
constraint is violated, Gold revenue calculations will silently incorporate
non-existent orders, compromising the tutorial's integrity.

See: https://github.com/OpenLakeForge/openlakeforge/issues/483
"""

from __future__ import annotations

import csv
from pathlib import Path


def test_crm_demo_order_lines_reference_existing_orders() -> None:
    """Every order_id in order_lines.csv must exist in orders.csv.

    This test validates referential integrity in the CRM demo fixtures to
    prevent orphaned order lines from skewing Gold revenue calculations in
    the product tutorial.
    """
    # Use checkout-anchored paths, not working directory
    repo_root = Path(__file__).resolve().parents[4]
    orders_path = repo_root / "lakehouse_code/bronze/crm/examples/orders.csv"
    order_lines_path = repo_root / "lakehouse_code/bronze/crm/examples/order_lines.csv"

    # Read all valid order IDs from orders.csv
    with orders_path.open(encoding="utf-8") as f:
        orders_reader = csv.DictReader(f)
        valid_order_ids = {row["order_id"] for row in orders_reader}

    # Validate each order line references a valid order
    orphaned_lines: list[tuple[str, str]] = []
    with order_lines_path.open(encoding="utf-8") as f:
        order_lines_reader = csv.DictReader(f)
        for row in order_lines_reader:
            line_id = row["line_id"]
            order_id = row["order_id"]
            if order_id not in valid_order_ids:
                orphaned_lines.append((line_id, order_id))

    # Report all orphaned lines in a single assertion
    assert not orphaned_lines, (
        f"Found {len(orphaned_lines)} orphaned order line(s) referencing non-existent orders:\n"
        + "\n".join(f"  {line_id} -> {order_id}" for line_id, order_id in orphaned_lines)
    )
