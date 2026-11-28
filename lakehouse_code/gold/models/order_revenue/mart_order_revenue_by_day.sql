select
    order_date,
    order_region as region,
    cast(count(distinct order_id) as bigint) as order_count,
    cast(sum(quantity) as bigint) as units_sold,
    cast(sum(gross_amount) as double) as gross_revenue,
    cast(sum(discount_amount) as double) as discount_amount,
    cast(sum(net_amount) as double) as net_revenue
from {{ ref('fct_order_lines') }}
where order_status = 'fulfilled'
group by order_date, order_region
