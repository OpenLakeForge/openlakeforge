with accounts as (
    select account_id, account_name, segment, region
    from {{ source('sales', 'accounts') }}
),

tickets as (
    select
        account_id,
        priority,
        cast(resolution_hours as integer) as resolution_hours,
        cast(sla_met as boolean) as sla_met
    from {{ source('sales', 'support_tickets') }}
)

select
    accounts.account_id,
    accounts.account_name,
    accounts.segment,
    accounts.region,
    tickets.priority,
    cast(count(*) as bigint) as ticket_count,
    cast(avg(tickets.resolution_hours) as double) as avg_resolution_hours,
    cast(sum(case when tickets.sla_met then 1 else 0 end) as bigint) as tickets_met_sla,
    cast(avg(case when tickets.sla_met then 1.0 else 0.0 end) as double) as sla_rate
from accounts
join tickets on accounts.account_id = tickets.account_id
group by accounts.account_id, accounts.account_name, accounts.segment, accounts.region, tickets.priority
