"""Render the descriptor-declared Source -> Domain -> Product -> Dashboard lineage as Mermaid."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openlakeforge_domain import LakehouseInventory

_UNSAFE_ID_CHARACTERS = re.compile(r"[^A-Za-z0-9_]")
# '#' goes first: Mermaid reads '#...;' as an entity, so escaping it later would mangle the other escapes.
_LABEL_ESCAPES = (("#", "#35;"), ('"', "#quot;"), ("<", "#lt;"), (">", "#gt;"))


def _node_id(kind: str, name: str) -> str:
    # The kind prefix keeps a domain and a product with the same name apart, and keeps
    # a bare name such as `end` from being read as Mermaid syntax.
    return f"{kind}_{_UNSAFE_ID_CHARACTERS.sub('_', name)}"


def _label(text: str) -> str:
    text = " ".join(text.split())
    for raw, escaped in _LABEL_ESCAPES:
        text = text.replace(raw, escaped)
    return f'"{text}"'


def render_lineage_graph(inventory: LakehouseInventory) -> str:
    """Return a Mermaid `flowchart LR` whose output is stable for unchanged descriptors."""
    nodes: dict[str, str] = {}
    edges: set[tuple[str, str]] = set()

    for source in inventory.sources:
        nodes[_node_id("source", source.name)] = source.display_name
    for domain in inventory.domains:
        domain_id = _node_id("domain", domain.name)
        nodes[domain_id] = domain.display_name
        for table in domain.silver_tables:
            edges.add((_node_id("source", table.source), domain_id))
        for product in domain.products:
            product_id = _node_id("product", product.id)
            nodes[product_id] = product.display_name
            edges.add((domain_id, product_id))
    for dashboard in inventory.dashboards:
        dashboard_id = _node_id("dashboard", dashboard.name)
        nodes[dashboard_id] = dashboard.name
        for product_name in dashboard.products:
            edges.add((_node_id("product", product_name), dashboard_id))

    lines = ["flowchart LR"]
    lines += [f"    {node_id}[{_label(nodes[node_id])}]" for node_id in sorted(nodes)]
    lines += [f"    {upstream} --> {downstream}" for upstream, downstream in sorted(edges)]
    return "\n".join(lines) + "\n"
