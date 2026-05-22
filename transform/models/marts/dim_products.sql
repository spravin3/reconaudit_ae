with events as (
    select * from {{ ref('int_events_enriched') }}
    where product_id is not null
),

add_to_cart_agg as (
    select
        product_id,
        count(*)                   as total_add_to_cart,
        count(distinct user_id)    as unique_users_carted
    from events
    where event_name = 'add_to_cart'
    group by product_id
),

purchase_intent_agg as (
    select
        product_id,
        count(*)                              as total_purchase_intents,
        count(distinct user_id)               as unique_users_intented,
        round(sum(intent_value), 2)           as total_intent_value,
        round(avg(intent_value), 2)           as avg_intent_value
    from events
    where event_name = 'purchase_intent'
    group by product_id
),

all_products as (
    select distinct product_id from events
)

select
    ap.product_id,
    coalesce(ca.total_add_to_cart, 0)          as total_add_to_cart,
    coalesce(ca.unique_users_carted, 0)         as unique_users_carted,
    coalesce(pi.total_purchase_intents, 0)      as total_purchase_intents,
    coalesce(pi.unique_users_intented, 0)       as unique_users_intented,
    coalesce(pi.total_intent_value, 0.0)        as total_intent_value,
    coalesce(pi.avg_intent_value, 0.0)          as avg_intent_value,
    case
        when coalesce(ca.total_add_to_cart, 0) = 0 then null
        else round(
            coalesce(pi.total_purchase_intents, 0)::double / ca.total_add_to_cart,
            3
        )
    end as cart_to_intent_rate
from all_products ap
left join add_to_cart_agg    ca on ap.product_id = ca.product_id
left join purchase_intent_agg pi on ap.product_id = pi.product_id
