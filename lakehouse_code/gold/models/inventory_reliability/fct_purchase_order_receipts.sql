-- Shipments are summed per purchase order so the grain stays one row per order even when an order ships in parts;
-- the left join keeps orders that have not shipped, with null delivery columns.
with shipments as (
    select
        purchase_order_id,
        cast(count(*) as integer) as shipment_count,
        min(cast(ship_date as date)) as ship_date,
        max(cast(delivered_date as date)) as delivered_date,
        cast(sum(received_units) as integer) as received_units
    from {{ source('supply_chain', 'shipments') }}
    group by purchase_order_id
)

select
    purchase_orders.purchase_order_id,
    purchase_orders.supplier_id,
    purchase_orders.product_id,
    purchase_orders.warehouse_id,
    cast(purchase_orders.order_date as date) as order_date,
    cast(purchase_orders.expected_date as date) as expected_date,
    cast(purchase_orders.ordered_units as integer) as ordered_units,
    coalesce(shipments.shipment_count, 0) as shipment_count,
    shipments.ship_date,
    shipments.delivered_date,
    shipments.received_units
from {{ source('supply_chain', 'purchase_orders') }} as purchase_orders
left join shipments on purchase_orders.purchase_order_id = shipments.purchase_order_id
