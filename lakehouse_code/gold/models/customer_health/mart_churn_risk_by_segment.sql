with health as (
    select *
    from {{ ref('mart_customer_health_score') }}
)

select
    segment,
    region,
    case
        when health_score < 55 then 'high'
        when health_score < 75 then 'medium'
        else 'low'
    end as churn_risk,
    cast(count(*) as bigint) as account_count,
    cast(sum(arr) as double) as arr_at_risk,
    cast(avg(health_score) as double) as avg_health_score
from health
group by
    segment,
    region,
    case
        when health_score < 55 then 'high'
        when health_score < 75 then 'medium'
        else 'low'
    end
