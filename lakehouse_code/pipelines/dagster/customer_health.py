from __future__ import annotations

from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec(domain="sales", product="customer_health")
defs = build_product_definitions(spec)
