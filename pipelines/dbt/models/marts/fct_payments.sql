-- Grain: one row per payment attempt (payment_id).
select
    p.payment_id,
    p.payment_event_id,
    p.order_id,
    o.customer_id,
    p.attempt_no,
    p.status,
    p.amount_paise,
    p.currency,
    p.method,
    p.event_ts as paid_at,
    p.payment_business_date,
    o.order_business_date
from {{ ref('stg_payments') }} p
left join {{ ref('stg_orders') }} o on o.order_id = p.order_id
