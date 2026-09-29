-- Grain: one row per order_id.
with events as (
    -- A re-delivered source event keeps its event_id; keep the first delivery only.
    select distinct on (event_id) *
    from {{ source('raw', 'raw_order_events') }}
    order by event_id, _load_id
),

created as (
    select order_id, customer_id, order_total_paise, currency, event_ts as created_at
    from events
    where event_type = 'order_created'
),

cancelled as (
    select order_id, min(event_ts) as cancelled_at
    from events
    where event_type = 'order_cancelled'
    group by order_id
)

select
    c.order_id,
    c.customer_id,
    c.order_total_paise,
    c.currency,
    c.created_at,
    {{ business_date('c.created_at') }} as order_business_date,
    x.cancelled_at,
    x.cancelled_at is not null as is_cancelled
from created c
left join cancelled x on x.order_id = c.order_id
