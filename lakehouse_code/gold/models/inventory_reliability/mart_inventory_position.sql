with ranked_snapshots as (
    select
        snapshot_date,
        warehouse_id,
        product_id,
        cast(on_hand_units as integer) as on_hand_units,
        cast(allocated_units as integer) as allocated_units,
        cast(reorder_point as integer) as reorder_point,
        row_number() over (
            partition by warehouse_id, product_id
            order by cast(snapshot_date as date) desc
        ) as snapshot_rank
    from {{ source('supply_chain', 'inventory_snapshots') }}
),

warehouses as (
    select warehouse_id, warehouse_name, region
    from {{ source('supply_chain', 'warehouses') }}
)

select
    ranked_snapshots.product_id,
    ranked_snapshots.warehouse_id,
    warehouses.warehouse_name,
    warehouses.region,
    ranked_snapshots.snapshot_date,
    ranked_snapshots.on_hand_units,
    ranked_snapshots.allocated_units,
    cast(ranked_snapshots.on_hand_units - ranked_snapshots.allocated_units as integer) as available_units,
    ranked_snapshots.reorder_point,
    case
        when ranked_snapshots.on_hand_units - ranked_snapshots.allocated_units <= ranked_snapshots.reorder_point then 'reorder'
        else 'healthy'
    end as inventory_status
from ranked_snapshots
join warehouses on ranked_snapshots.warehouse_id = warehouses.warehouse_id
where ranked_snapshots.snapshot_rank = 1
