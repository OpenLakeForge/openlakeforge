with orders as (
    select
        order_id,
        channel_id,
        promotion_id,
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
),

channels as (
    select channel_id, channel_name, channel_type
    from {{ source('sales', 'channels') }}
),

promotions as (
    select promotion_id, promotion_name, discount_type
    from {{ source('sales', 'promotions') }}
)

select
    channels.channel_name,
    channels.channel_type,
    promotions.discount_type,
    orders.region,
    cast(count(distinct orders.order_id) as bigint) as order_count,
    cast(sum(order_lines.quantity * order_lines.unit_price - order_lines.discount_amount) as double) as net_revenue,
    cast(sum(order_lines.discount_amount) as double) as discount_amount
from orders
join order_lines on orders.order_id = order_lines.order_id
join channels on orders.channel_id = channels.channel_id
join promotions on orders.promotion_id = promotions.promotion_id
group by channels.channel_name, channels.channel_type, promotions.discount_type, orders.region
