from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductCadence, ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec(
    domain="supply_chain",
    product="inventory_reliability",
    cadence=ProductCadence(cron="0 3 * * *", enabled_in_prod=True),
)
defs = build_product_definitions(spec)
