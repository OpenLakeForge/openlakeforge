select
    snapshot_id,
    cast(snapshot_date as date) as snapshot_date,
    warehouse_id,
    product_id,
    cast(on_hand_units as integer) as on_hand_units,
    cast(allocated_units as integer) as allocated_units,
    cast(reorder_point as integer) as reorder_point
from {{ source('supply_chain', 'inventory_snapshots') }}
