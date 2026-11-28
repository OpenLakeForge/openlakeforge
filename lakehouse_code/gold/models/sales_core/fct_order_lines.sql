-- Left join keeps one row per Silver order line even if its header is missing;
-- the not_null and relationships tests then report the orphan.
with order_lines as (
    select
        line_id,
        order_id,
        product_id,
        cast(quantity as integer) as quantity,
        cast(unit_price as decimal(18, 4)) as unit_price,
        cast(discount_amount as decimal(18, 2)) as discount_amount
    from {{ source('sales', 'order_lines') }}
),

orders as (
    select
        order_id,
        cast(order_date as date) as order_date,
        status as order_status,
        region as order_region,
        customer_id as account_id,
        channel_id,
        promotion_id
    from {{ source('sales', 'orders') }}
),

products as (
    select
        product_id,
        cast(unit_cost as decimal(18, 4)) as unit_cost
    from {{ source('sales', 'products') }}
)

select
    order_lines.line_id,
    order_lines.order_id,
    orders.order_date,
    orders.order_status,
    orders.order_region,
    orders.account_id,
    orders.channel_id,
    orders.promotion_id,
    order_lines.product_id,
    order_lines.quantity,
    order_lines.unit_price,
    cast(order_lines.quantity * order_lines.unit_price as decimal(18, 2)) as gross_amount,
    order_lines.discount_amount,
    cast(order_lines.quantity * order_lines.unit_price - order_lines.discount_amount as decimal(18, 2)) as net_amount,
    cast(order_lines.quantity * products.unit_cost as decimal(18, 2)) as cost_amount
from order_lines
left join orders on order_lines.order_id = orders.order_id
left join products on order_lines.product_id = products.product_id
