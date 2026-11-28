select
    warehouse_id,
    warehouse_name,
    region as warehouse_region,
    cast(capacity_units as integer) as capacity_units
from {{ source('supply_chain', 'warehouses') }}
