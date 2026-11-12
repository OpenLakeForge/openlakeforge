with inventory_position as (
    select *
    from {{ ref('mart_inventory_position') }}
),

stockouts as (
    select
        warehouse_id,
        product_id,
        cast(lost_units as integer) as lost_units
    from {{ source('supply_chain', 'stockout_events') }}
),

stockout_rollup as (
    select
        warehouse_id,
        product_id,
        cast(count(*) as bigint) as stockout_event_count,
        cast(sum(lost_units) as bigint) as lost_units
    from stockouts
    group by warehouse_id, product_id
)

select
    inventory_position.product_id,
    inventory_position.warehouse_id,
    inventory_position.warehouse_name,
    inventory_position.region,
    inventory_position.available_units,
    inventory_position.reorder_point,
    inventory_position.inventory_status,
    coalesce(stockout_rollup.stockout_event_count, 0) as stockout_event_count,
    coalesce(stockout_rollup.lost_units, 0) as lost_units,
    case
        when inventory_position.inventory_status = 'reorder' and coalesce(stockout_rollup.stockout_event_count, 0) >= 1 then 'high'
        when inventory_position.inventory_status = 'reorder' then 'medium'
        else 'low'
    end as stockout_risk
from inventory_position
left join stockout_rollup
    on inventory_position.warehouse_id = stockout_rollup.warehouse_id
    and inventory_position.product_id = stockout_rollup.product_id
