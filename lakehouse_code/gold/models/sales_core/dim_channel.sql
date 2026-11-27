select
    channel_id,
    channel_name,
    channel_type
from {{ source('sales', 'channels') }}
