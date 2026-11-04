from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions

CUSTOMER_HEALTH_SILVER_INPUTS = ("accounts", "subscriptions", "support_tickets", "nps_responses")

CUSTOMER_HEALTH_GOLD_ASSETS = (
    "mart_customer_health_score",
    "mart_churn_risk_by_segment",
    "mart_support_sla_by_customer",
)


defs = build_product_definitions(
    ProductDefinitionSpec(
        domain="sales",
        product="customer_health",
        silver_inputs=CUSTOMER_HEALTH_SILVER_INPUTS,
        bronze_inputs=tuple(("crm", entity) for entity in CUSTOMER_HEALTH_SILVER_INPUTS),
        gold_assets=CUSTOMER_HEALTH_GOLD_ASSETS,
    )
)
