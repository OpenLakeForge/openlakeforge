from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductCadence, ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec(
    domain="sales",
    product="order_revenue",
    cadence=ProductCadence(cron="0 2 * * *", enabled_in_prod=True),
)
defs = build_product_definitions(spec)
