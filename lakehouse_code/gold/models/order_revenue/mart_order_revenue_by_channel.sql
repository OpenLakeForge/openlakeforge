select
    dim_channel.channel_name,
    dim_channel.channel_type,
    dim_promotion.discount_type,
    fct_order_lines.order_region as region,
    cast(count(distinct fct_order_lines.order_id) as bigint) as order_count,
    cast(sum(fct_order_lines.net_amount) as double) as net_revenue,
    cast(sum(fct_order_lines.discount_amount) as double) as discount_amount
from {{ ref('fct_order_lines') }} as fct_order_lines
join {{ ref('dim_channel') }} as dim_channel on fct_order_lines.channel_id = dim_channel.channel_id
join {{ ref('dim_promotion') }} as dim_promotion on fct_order_lines.promotion_id = dim_promotion.promotion_id
where fct_order_lines.order_status = 'fulfilled'
group by dim_channel.channel_name, dim_channel.channel_type, dim_promotion.discount_type, fct_order_lines.order_region
