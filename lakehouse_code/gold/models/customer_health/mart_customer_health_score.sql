with accounts as (
    select
        account_id,
        account_name,
        segment,
        account_region as region,
        cast(arr as double) as arr
    from {{ ref('dim_account') }}
),

subscriptions as (
    select
        account_id,
        status,
        cast(monthly_recurring_revenue as double) as monthly_recurring_revenue
    from {{ ref('fct_subscriptions') }}
),

tickets as (
    select
        account_id,
        resolution_hours,
        sla_met
    from {{ ref('fct_support_tickets') }}
),

nps as (
    select
        account_id,
        score
    from {{ ref('fct_nps_responses') }}
),

ticket_rollup as (
    select
        account_id,
        cast(count(*) as bigint) as ticket_count,
        cast(avg(resolution_hours) as double) as avg_resolution_hours,
        cast(avg(case when sla_met then 1.0 else 0.0 end) as double) as sla_rate
    from tickets
    group by account_id
),

nps_rollup as (
    select
        account_id,
        cast(avg(score) as double) as avg_nps
    from nps
    group by account_id
)

select
    accounts.account_id,
    accounts.account_name,
    accounts.segment,
    accounts.region,
    accounts.arr,
    subscriptions.status as subscription_status,
    subscriptions.monthly_recurring_revenue,
    coalesce(ticket_rollup.ticket_count, 0) as ticket_count,
    coalesce(ticket_rollup.avg_resolution_hours, 0.0) as avg_resolution_hours,
    coalesce(ticket_rollup.sla_rate, 1.0) as sla_rate,
    coalesce(nps_rollup.avg_nps, 7.0) as avg_nps,
    cast(
        50
        + coalesce(nps_rollup.avg_nps, 7.0) * 3
        + coalesce(ticket_rollup.sla_rate, 1.0) * 20
        - coalesce(ticket_rollup.ticket_count, 0) * 2
        - case when subscriptions.status = 'past_due' then 20 else 0 end
        as double
    ) as health_score
from accounts
join subscriptions on accounts.account_id = subscriptions.account_id
left join ticket_rollup on accounts.account_id = ticket_rollup.account_id
left join nps_rollup on accounts.account_id = nps_rollup.account_id
