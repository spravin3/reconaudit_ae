-- User-day grain. Full funnel from first page_view through to server-side conversion.
-- page_view is the real funnel entry point; had_page_view=false means attribution gap.
with events as (
    select * from {{ ref('int_events_enriched') }}
),

server_txns as (
    select * from {{ ref('stg_server_logs') }}
),

client_daily as (
    select
        user_id,
        event_timestamp::date                                                          as activity_date,
        count(*) filter (where event_name = 'page_view')                               as page_views,
        count(*) filter (where event_name = 'add_to_cart')                             as add_to_cart_count,
        count(*) filter (where event_name = 'purchase_intent')                         as purchase_intent_count,
        count(distinct product_id) filter (where event_name = 'add_to_cart')           as unique_products_carted,
        count(distinct product_id) filter (where event_name = 'purchase_intent')       as unique_products_intented,
        round(sum(intent_value) filter (where event_name = 'purchase_intent'), 2)      as total_intent_value,
        mode(traffic_source)                                                            as primary_traffic_source,
        mode(channel_type)                                                              as primary_channel_type,
        bool_or(is_paid)                                                                as any_paid_session
    from events
    group by user_id, event_timestamp::date
),

server_daily as (
    select
        user_id,
        tx_timestamp::date                                              as activity_date,
        count(*)                                                        as total_txns,
        count(*) filter (where status = 'completed')                    as completed_txns,
        count(*) filter (where status = 'failed')                       as failed_txns,
        round(sum(amount) filter (where status = 'completed'), 2)       as server_revenue
    from server_txns
    group by user_id, tx_timestamp::date
),

all_user_dates as (
    select user_id, activity_date from client_daily
    union
    select user_id, activity_date from server_daily
)

select
    ud.user_id,
    ud.activity_date,

    -- top-of-funnel (awareness)
    coalesce(cd.page_views, 0)                    as page_views,

    -- mid-funnel (consideration)
    coalesce(cd.add_to_cart_count, 0)             as add_to_cart_count,
    coalesce(cd.unique_products_carted, 0)         as unique_products_carted,

    -- bottom-of-funnel (intent)
    coalesce(cd.purchase_intent_count, 0)          as purchase_intent_count,
    coalesce(cd.unique_products_intented, 0)       as unique_products_intented,
    coalesce(cd.total_intent_value, 0.0)           as total_intent_value,

    -- acquisition context
    cd.primary_traffic_source,
    cd.primary_channel_type,
    coalesce(cd.any_paid_session, false)           as any_paid_session,

    -- server-side outcome
    coalesce(sd.total_txns, 0)                     as total_txns,
    coalesce(sd.completed_txns, 0)                 as completed_txns,
    coalesce(sd.failed_txns, 0)                    as failed_txns,
    coalesce(sd.server_revenue, 0.0)               as server_revenue,

    -- funnel stage flags (use for funnel drop-off analysis)
    coalesce(cd.page_views, 0) > 0                 as had_page_view,
    coalesce(cd.add_to_cart_count, 0) > 0          as had_add_to_cart,
    coalesce(cd.purchase_intent_count, 0) > 0      as had_purchase_intent,
    coalesce(sd.completed_txns, 0) > 0             as had_conversion

from all_user_dates ud
left join client_daily cd on ud.user_id = cd.user_id and ud.activity_date = cd.activity_date
left join server_daily sd on ud.user_id = sd.user_id and ud.activity_date = sd.activity_date
