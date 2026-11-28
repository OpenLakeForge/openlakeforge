select
    promotion_id,
    promotion_name,
    discount_type
from {{ source('sales', 'promotions') }}
