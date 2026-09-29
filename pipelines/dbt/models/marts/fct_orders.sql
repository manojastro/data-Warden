-- Grain: one row per order_id.
select
    o.order_id,
    o.customer_id,
    c.city as customer_city,
    o.order_total_paise,
    o.currency,
    o.created_at,
    o.order_business_date,
    o.is_cancelled,
    o.cancelled_at
from {{ ref('stg_orders') }} o
left join {{ ref('dim_customers') }} c on c.customer_id = o.customer_id
