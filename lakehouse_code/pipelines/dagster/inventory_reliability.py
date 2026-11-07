from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions

defs = build_product_definitions(ProductDefinitionSpec(domain="supply_chain", product="inventory_reliability"))
