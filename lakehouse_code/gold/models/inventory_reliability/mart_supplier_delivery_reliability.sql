with receipts as (
    select
        purchase_order_id,
        supplier_id,
        expected_date,
        ordered_units,
        delivered_date,
        received_units
    from {{ ref('fct_purchase_order_receipts') }}
    where delivered_date is not null
),

suppliers as (
    select supplier_id, supplier_name, country, risk_tier
    from {{ ref('dim_supplier') }}
)

select
    suppliers.supplier_id,
    suppliers.supplier_name,
    suppliers.country,
    suppliers.risk_tier,
    cast(count(*) as bigint) as purchase_order_count,
    cast(sum(receipts.ordered_units) as bigint) as ordered_units,
    cast(sum(receipts.received_units) as bigint) as received_units,
    cast(avg(date_diff('day', receipts.expected_date, receipts.delivered_date)) as double) as avg_days_late,
    cast(avg(case when receipts.delivered_date <= receipts.expected_date then 1.0 else 0.0 end) as double) as on_time_rate
from receipts
join suppliers on receipts.supplier_id = suppliers.supplier_id
group by suppliers.supplier_id, suppliers.supplier_name, suppliers.country, suppliers.risk_tier
