with client_summary as (
    select
        user_id,
        min(event_timestamp)                                              as first_event_at,
        max(event_timestamp)                                              as last_event_at,
        count(*)                                                          as total_events,
        count(*) filter (where event_name = 'page_view')                  as page_views,
        count(*) filter (where event_name = 'add_to_cart')                as add_to_cart_count,
        count(*) filter (where event_name = 'purchase_intent')            as purchase_intent_count,
        mode(traffic_source)                                              as primary_traffic_source
    from {{ ref('int_events_enriched') }}
    group by user_id
),

server_summary as (
    select * from {{ ref('int_user_transaction_summary') }}
),

all_users as (
    select user_id from {{ ref('int_events_enriched') }}
    union
    select user_id from {{ ref('stg_server_logs') }}
)

select
    u.user_id,
    cs.first_event_at,
    cs.last_event_at,
    coalesce(cs.total_events, 0)              as total_events,
    coalesce(cs.page_views, 0)                as page_views,
    coalesce(cs.add_to_cart_count, 0)         as add_to_cart_count,
    coalesce(cs.purchase_intent_count, 0)     as purchase_intent_count,
    cs.primary_traffic_source,
    coalesce(ss.total_txns, 0)                as total_txns,
    coalesce(ss.completed_txns, 0)            as completed_txns,
    coalesce(ss.failed_txns, 0)               as failed_txns,
    coalesce(ss.total_revenue, 0.0)           as total_revenue,
    coalesce(ss.avg_tx_amount, 0.0)           as avg_tx_amount,
    ss.first_tx_at,
    ss.last_tx_at,
    (cs.user_id is not null)                  as has_client_activity,
    (ss.user_id is not null)                  as has_server_activity
from all_users u
left join client_summary cs on u.user_id = cs.user_id
left join server_summary  ss on u.user_id = ss.user_id
