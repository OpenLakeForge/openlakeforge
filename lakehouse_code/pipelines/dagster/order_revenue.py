from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions

ORDER_REVENUE_SILVER_INPUTS = ("orders", "order_lines", "products", "channels", "promotions", "accounts")

ORDER_REVENUE_GOLD_ASSETS = (
    "mart_order_revenue_by_day",
    "mart_order_revenue_by_channel",
    "mart_order_revenue_margin_by_product",
    "mart_order_revenue_by_account_segment",
)

defs = build_product_definitions(
    ProductDefinitionSpec(
        domain="sales",
        product="order_revenue",
        silver_inputs=ORDER_REVENUE_SILVER_INPUTS,
        bronze_inputs=tuple(("crm", entity) for entity in ORDER_REVENUE_SILVER_INPUTS),
        gold_assets=ORDER_REVENUE_GOLD_ASSETS,
    )
)
