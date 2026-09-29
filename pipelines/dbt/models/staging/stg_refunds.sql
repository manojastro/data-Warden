-- Grain: one row per refund event (re-delivered events are collapsed on event_id).
with events as (
    select distinct on (event_id) *
    from {{ source('raw', 'raw_refund_events') }}
    order by event_id, _load_id
)

select
    event_id as refund_event_id,
    refund_id,
    payment_id,
    order_id,
    amount_paise,
    currency,
    status,
    event_ts,
    {{ business_date('event_ts') }} as refund_business_date,
    _batch_id as batch_id
from events
