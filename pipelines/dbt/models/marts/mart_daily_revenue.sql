{{ config(
    materialized='incremental',
    unique_key='business_date',
    incremental_strategy='delete+insert',
    on_schema_change='fail'
) }}

-- Grain: one row per business_date (Asia/Kolkata).
-- gross collected = captured payments by payment business date
-- refunds         = succeeded refunds by refund business date
-- net revenue     = gross collected - refunds
-- Payments and refunds are aggregated independently before joining, so neither multiplies
-- the other. Scheduled runs recompute the latest `lookback_days` partitions; older
-- partitions change only through an explicit replay window (replay_start/replay_end).

with bounds as (
    {% if is_incremental() %}
        {% if var('replay_start') and var('replay_end') %}
    select '{{ var("replay_start") }}'::date as start_date, '{{ var("replay_end") }}'::date as end_date
        {% else %}
    select (max(d) - {{ var('lookback_days') }})::date as start_date, max(d) as end_date
    from (
        select max(payment_business_date) as d from {{ ref('fct_payments') }}
        union all
        select max(refund_business_date) from {{ ref('fct_refunds') }}
    ) latest
        {% endif %}
    {% else %}
    select '1900-01-01'::date as start_date, '2999-12-31'::date as end_date
    {% endif %}
),

payments as (
    select
        payment_business_date as business_date,
        sum(amount_paise) as gross_collected_paise,
        count(*) as captured_payment_count
    from {{ ref('fct_payments') }}
    cross join bounds
    where status = 'captured'
      and payment_business_date between bounds.start_date and bounds.end_date
    group by payment_business_date
),

refunds as (
    select
        refund_business_date as business_date,
        sum(amount_paise) as refunds_paise,
        count(*) as refund_count
    from {{ ref('fct_refunds') }}
    cross join bounds
    where status = 'succeeded'
      and refund_business_date between bounds.start_date and bounds.end_date
    group by refund_business_date
),

dates as (
    select business_date from payments
    union
    select business_date from refunds
)

select
    d.business_date,
    coalesce(p.gross_collected_paise, 0)::bigint as gross_collected_paise,
    coalesce(r.refunds_paise, 0)::bigint as refunds_paise,
    (coalesce(p.gross_collected_paise, 0) - coalesce(r.refunds_paise, 0))::bigint as net_revenue_paise,
    coalesce(p.captured_payment_count, 0)::integer as captured_payment_count,
    coalesce(r.refund_count, 0)::integer as refund_count,
    '{{ var("code_version") }}'::text as code_version,
    now() as computed_at
from dates d
left join payments p on p.business_date = d.business_date
left join refunds r on r.business_date = d.business_date
