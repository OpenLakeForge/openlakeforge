from __future__ import annotations

from openlakeforge_runtime.code_location import ProductOverrides
from openlakeforge_runtime.product_dagster import ProductCadence

# A product needs an entry only to depart from its defaults, which the descriptors already supply: its job is built
# from governance.yml, its inputs from the dbt and Floe projects, and without a cadence its PROD schedule is daily at
# 03:00 UTC and ships STOPPED. Set enabled_in_prod=True to have PROD run the schedule on deploy. Pass gold_assets,
# silver_inputs or bronze_inputs only to override what the projects declare.
# The sales products share Silver tables, so their runs are staggered. sales_core runs first because the facts and
# dimensions it owns are what the other sales products are meant to read.
PRODUCTS: dict[str, ProductOverrides] = {
    "sales_core": ProductOverrides(cadence=ProductCadence(cron="30 1 * * *", enabled_in_prod=True)),
    "order_revenue": ProductOverrides(cadence=ProductCadence(cron="0 2 * * *", enabled_in_prod=True)),
    "customer_health": ProductOverrides(cadence=ProductCadence(cron="30 2 * * *", enabled_in_prod=True)),
    "inventory_reliability": ProductOverrides(cadence=ProductCadence(cron="0 3 * * *", enabled_in_prod=True)),
}
