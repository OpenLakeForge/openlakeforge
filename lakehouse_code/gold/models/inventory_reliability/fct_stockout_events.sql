select
    event_id,
    cast(event_date as date) as event_date,
    warehouse_id,
    product_id,
    cast(lost_units as integer) as lost_units,
    root_cause
from {{ source('supply_chain', 'stockout_events') }}
