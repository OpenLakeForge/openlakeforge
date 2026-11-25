"""Format and render a concise, deterministic summary of a lakehouse project."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openlakeforge_core import LakehouseInventory


def project_summary_dict(inventory: LakehouseInventory) -> dict[str, Any]:
    """Extract a sorted, deterministic inventory summary as a dictionary."""
    return {
        "dashboards": sorted(dashboard.name for dashboard in inventory.dashboards),
        "domains": [
            {
                "name": domain.name,
                "silver_tables": len(inventory.floe_entities(domain)),
            }
            for domain in sorted(inventory.domains, key=lambda d: d.name)
        ],
        "products": [
            {
                "gold_marts": len(inventory.gold_marts(product)),
                "name": product.name,
            }
            for product in sorted(inventory.products, key=lambda p: p.name)
        ],
        "sources": [
            {
                "name": source.name,
                "resources": sorted(table.name for table in source.resources),
            }
            for source in sorted(inventory.sources, key=lambda s: s.name)
        ],
    }


def render_project_summary_text(inventory: LakehouseInventory) -> str:
    """Render the project inventory summary as human-readable text."""
    summary = project_summary_dict(inventory)
    lines: list[str] = ["Sources:"]
    for source in summary["sources"]:
        resources_str = ", ".join(source["resources"]) if source["resources"] else "(none)"
        lines.append(f"  {source['name']}: {resources_str}")
    if not summary["sources"]:
        lines.append("  (none)")

    lines.append("Domains:")
    for domain in summary["domains"]:
        lines.append(f"  {domain['name']}: {domain['silver_tables']} Silver table(s)")
    if not summary["domains"]:
        lines.append("  (none)")

    lines.append("Products:")
    for product in summary["products"]:
        lines.append(f"  {product['name']}: {product['gold_marts']} Gold mart(s)")
    if not summary["products"]:
        lines.append("  (none)")

    lines.append("Dashboards:")
    for dashboard in summary["dashboards"]:
        lines.append(f"  {dashboard}")
    if not summary["dashboards"]:
        lines.append("  (none)")

    return "\n".join(lines) + "\n"


def render_project_summary_json(inventory: LakehouseInventory) -> str:
    """Render the project inventory summary as deterministic JSON."""
    return json.dumps(project_summary_dict(inventory), indent=2, sort_keys=True) + "\n"
