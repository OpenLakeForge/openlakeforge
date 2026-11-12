with orders as (
    select
        order_id,
        cast(order_date as date) as order_date,
        region
    from {{ source('sales', 'orders') }}
    where status = 'fulfilled'
),

order_lines as (
    select
        order_id,
        cast(quantity as integer) as quantity,
        cast(unit_price as double) as unit_price,
        cast(discount_amount as double) as discount_amount
    from {{ source('sales', 'order_lines') }}
)

select
    orders.order_date,
    orders.region,
    cast(count(distinct orders.order_id) as bigint) as order_count,
    cast(sum(order_lines.quantity) as bigint) as units_sold,
    cast(sum(order_lines.quantity * order_lines.unit_price) as double) as gross_revenue,
    cast(sum(order_lines.discount_amount) as double) as discount_amount,
    cast(sum(order_lines.quantity * order_lines.unit_price - order_lines.discount_amount) as double) as net_revenue
from orders
join order_lines on orders.order_id = order_lines.order_id
group by orders.order_date, orders.region
