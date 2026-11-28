with ranked_snapshots as (
    select
        snapshot_date,
        warehouse_id,
        product_id,
        on_hand_units,
        allocated_units,
        reorder_point,
        row_number() over (
            partition by warehouse_id, product_id
            order by snapshot_date desc
        ) as snapshot_rank
    from {{ ref('fct_inventory_snapshots') }}
),

warehouses as (
    select warehouse_id, warehouse_name, warehouse_region as region
    from {{ ref('dim_warehouse') }}
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
