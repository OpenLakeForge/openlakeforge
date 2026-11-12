with order_lines as (
    select
        product_id,
        cast(quantity as integer) as quantity,
        cast(unit_price as double) as unit_price,
        cast(discount_amount as double) as discount_amount
    from {{ source('sales', 'order_lines') }}
),

products as (
    select
        product_id,
        product_name,
        category,
        cast(unit_cost as double) as unit_cost
    from {{ source('sales', 'products') }}
)

select
    products.product_id,
    products.product_name,
    products.category,
    cast(count(*) as bigint) as line_count,
    cast(sum(order_lines.quantity) as bigint) as units_sold,
    cast(sum(order_lines.quantity * order_lines.unit_price - order_lines.discount_amount) as double) as net_revenue,
    cast(sum(order_lines.quantity * products.unit_cost) as double) as total_cost,
    cast(sum(order_lines.quantity * order_lines.unit_price - order_lines.discount_amount - order_lines.quantity * products.unit_cost) as double) as gross_margin
from order_lines
join products on order_lines.product_id = products.product_id
group by products.product_id, products.product_name, products.category
