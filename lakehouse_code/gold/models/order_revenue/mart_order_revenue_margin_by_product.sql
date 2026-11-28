select
    dim_product.product_id,
    dim_product.product_name,
    dim_product.category,
    cast(count(*) as bigint) as line_count,
    cast(sum(fct_order_lines.quantity) as bigint) as units_sold,
    cast(sum(fct_order_lines.net_amount) as double) as net_revenue,
    cast(sum(fct_order_lines.cost_amount) as double) as total_cost,
    cast(sum(fct_order_lines.net_amount - fct_order_lines.cost_amount) as double) as gross_margin
from {{ ref('fct_order_lines') }} as fct_order_lines
join {{ ref('dim_product') }} as dim_product on fct_order_lines.product_id = dim_product.product_id
where fct_order_lines.order_status = 'fulfilled'
group by dim_product.product_id, dim_product.product_name, dim_product.category
