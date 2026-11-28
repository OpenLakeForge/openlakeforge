select
    ticket_id,
    account_id,
    cast(opened_date as date) as opened_date,
    priority,
    cast(resolution_hours as integer) as resolution_hours,
    cast(sla_met as boolean) as sla_met
from {{ source('sales', 'support_tickets') }}
