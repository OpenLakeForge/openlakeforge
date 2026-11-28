select
    dim_account.segment,
    dim_account.account_region as region,
    cast(count(distinct fct_order_lines.order_id) as bigint) as order_count,
    cast(sum(fct_order_lines.net_amount) as double) as net_revenue
from {{ ref('fct_order_lines') }} as fct_order_lines
join {{ ref('dim_account') }} as dim_account on fct_order_lines.account_id = dim_account.account_id
where fct_order_lines.order_status = 'fulfilled'
group by dim_account.segment, dim_account.account_region
