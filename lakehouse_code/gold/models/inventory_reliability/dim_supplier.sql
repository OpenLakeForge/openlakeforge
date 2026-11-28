select
    supplier_id,
    supplier_name,
    country,
    risk_tier
from {{ source('supply_chain', 'suppliers') }}
