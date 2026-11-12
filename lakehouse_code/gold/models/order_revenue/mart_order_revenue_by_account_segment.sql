with orders as (
    select
        order_id,
        customer_id,
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

accounts as (
    select account_id, segment, region as account_region
    from {{ source('sales', 'accounts') }}
)

select
    accounts.segment,
    accounts.account_region as region,
    cast(count(distinct orders.order_id) as bigint) as order_count,
    cast(sum(order_lines.quantity * order_lines.unit_price - order_lines.discount_amount) as double) as net_revenue
from orders
join order_lines on orders.order_id = order_lines.order_id
join accounts on orders.customer_id = accounts.account_id
group by accounts.segment, accounts.account_region
