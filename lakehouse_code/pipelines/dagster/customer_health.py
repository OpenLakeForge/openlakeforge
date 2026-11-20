from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductCadence, ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec(
    domain="sales",
    product="customer_health",
    cadence=ProductCadence(cron="30 2 * * *", enabled_in_prod=True),
)
defs = build_product_definitions(spec)
