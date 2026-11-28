select
    subscription_id,
    account_id,
    plan_name,
    status,
    cast(start_date as date) as start_date,
    cast(renewal_date as date) as renewal_date,
    cast(monthly_recurring_revenue as decimal(18, 2)) as monthly_recurring_revenue
from {{ source('sales', 'subscriptions') }}
