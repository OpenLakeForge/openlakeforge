{#- Keeps a mart from drifting off its fact: the mart's net_revenue total must equal the fulfilled net_amount total of
    sales_core.fct_order_lines. The mart is double and the fact decimal(18,2), hence the tolerance. -#}
{% test net_revenue_matches_fact(model, column_name='net_revenue', tolerance=0.01) %}
with mart as (
    select coalesce(sum({{ column_name }}), 0) as total from {{ model }}
),

fact as (
    select coalesce(sum(net_amount), 0) as total
    from {{ ref('fct_order_lines') }}
    where order_status = 'fulfilled'
)

select mart.total as mart_total, fact.total as fact_total
from mart
cross join fact
where abs(mart.total - cast(fact.total as double)) > {{ tolerance }}
{% endtest %}
