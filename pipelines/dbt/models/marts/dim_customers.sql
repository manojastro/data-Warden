-- Grain: one row per customer_id. Personal fields (name, email, phone) are deliberately excluded.
with events as (
    select distinct on (event_id) event_id, customer_id, city, event_ts, _load_id
    from {{ source('raw', 'raw_customer_events') }}
    order by event_id, _load_id
),

latest as (
    select distinct on (customer_id) customer_id, city, event_ts as updated_at
    from events
    order by customer_id, event_ts desc, event_id desc
),

first_seen as (
    select customer_id, min(event_ts) as first_seen_at from events group by customer_id
)

select l.customer_id, l.city, f.first_seen_at, l.updated_at
from latest l
join first_seen f on f.customer_id = l.customer_id
