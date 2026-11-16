from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec(domain="sales", product="order_revenue")
defs = build_product_definitions(spec)
