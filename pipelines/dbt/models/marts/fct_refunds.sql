-- Grain: one row per refund_id (latest status event wins).
select distinct on (refund_id)
    refund_id,
    refund_event_id,
    payment_id,
    order_id,
    amount_paise,
    currency,
    status,
    event_ts as refunded_at,
    refund_business_date
from {{ ref('stg_refunds') }}
order by refund_id, event_ts desc, refund_event_id desc
