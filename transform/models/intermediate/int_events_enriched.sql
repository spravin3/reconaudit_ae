-- Client events joined to seed lookups to add funnel metadata and channel classification.
-- Shared input for dim_users, dim_products, and fct_user_journey.
with events as (
    select * from {{ ref('stg_client_events') }}
),

event_types as (
    select * from {{ ref('dim_event_types') }}
),

traffic_sources as (
    select * from {{ ref('dim_traffic_sources') }}
)

select
    e.event_id,
    e.event_timestamp,
    e.user_id,
    e.event_name,
    e.url,
    e.traffic_source,
    e.product_id,
    e.intent_value,
    et.funnel_stage,
    et.funnel_order,
    ts.channel_type,
    ts.is_paid
from events e
left join event_types     et on e.event_name      = et.event_name
left join traffic_sources ts on e.traffic_source  = ts.traffic_source
