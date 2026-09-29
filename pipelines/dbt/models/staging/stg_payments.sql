-- Grain: one row per payment event received from the source.
select
    event_id as payment_event_id,
    payment_id,
    order_id,
    attempt_no,
    status,
    amount_paise,
    currency,
    method,
    event_ts,
    {{ business_date('event_ts') }} as payment_business_date,
    _batch_id as batch_id,
    _ingested_at as ingested_at
from {{ source('raw', 'raw_payment_events') }}
