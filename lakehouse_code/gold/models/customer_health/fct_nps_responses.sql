select
    response_id,
    account_id,
    cast(response_date as date) as response_date,
    cast(score as integer) as score
from {{ source('sales', 'nps_responses') }}
