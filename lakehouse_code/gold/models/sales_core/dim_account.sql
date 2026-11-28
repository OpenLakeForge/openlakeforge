select
    account_id,
    account_name,
    segment,
    region as account_region,
    cast(signup_date as date) as signup_date,
    cast(arr as decimal(18, 2)) as arr
from {{ source('sales', 'accounts') }}
