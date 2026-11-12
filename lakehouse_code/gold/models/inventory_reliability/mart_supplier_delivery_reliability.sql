with purchase_orders as (
    select
        purchase_order_id,
        supplier_id,
        product_id,
        warehouse_id,
        cast(expected_date as date) as expected_date,
        cast(ordered_units as integer) as ordered_units
    from {{ source('supply_chain', 'purchase_orders') }}
),

shipments as (
    select
        purchase_order_id,
        cast(delivered_date as date) as delivered_date,
        cast(received_units as integer) as received_units
    from {{ source('supply_chain', 'shipments') }}
),

suppliers as (
    select supplier_id, supplier_name, country, risk_tier
    from {{ source('supply_chain', 'suppliers') }}
)

select
    suppliers.supplier_id,
    suppliers.supplier_name,
    suppliers.country,
    suppliers.risk_tier,
    cast(count(*) as bigint) as purchase_order_count,
    cast(sum(purchase_orders.ordered_units) as bigint) as ordered_units,
    cast(sum(shipments.received_units) as bigint) as received_units,
    cast(avg(date_diff('day', purchase_orders.expected_date, shipments.delivered_date)) as double) as avg_days_late,
    cast(avg(case when shipments.delivered_date <= purchase_orders.expected_date then 1.0 else 0.0 end) as double) as on_time_rate
from purchase_orders
join shipments on purchase_orders.purchase_order_id = shipments.purchase_order_id
join suppliers on purchase_orders.supplier_id = suppliers.supplier_id
group by suppliers.supplier_id, suppliers.supplier_name, suppliers.country, suppliers.risk_tier
