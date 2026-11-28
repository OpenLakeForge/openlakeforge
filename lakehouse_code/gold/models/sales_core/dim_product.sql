select
    product_id,
    product_name,
    category,
    cast(unit_cost as decimal(18, 4)) as unit_cost
from {{ source('sales', 'products') }}
