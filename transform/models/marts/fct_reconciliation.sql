{{
  config(materialized='table')
}}

/*
  Fact table: one row per server transaction, enriched with matching client event.

  Join key: server.ext_id = client.event_id
  Only purchase_intent events are eligible to join (add_to_cart has no monetary value).

  reconciliation_status taxonomy:
    confirmed_revenue       - completed server tx with matching client purchase_intent
    intent_but_failed       - purchase_intent fired on client, server tx failed
    unattributed_server_tx  - completed server tx with no client event at all (dark block)
    failed_unattributed     - failed server tx with no client event (fully dark failure)

  amount_delta: server.amount - client.intent_value
    Should be 0.00 for all linked records; any non-zero value is a pricing anomaly.
*/

with client_intent as (
    select
        event_id,
        event_timestamp,
        user_id,
        product_id,
        intent_value
    from {{ ref('stg_client_events') }}
    where event_name = 'purchase_intent'
),

server as (
    select * from {{ ref('stg_server_logs') }}
),

joined as (
    select
        -- server side (always present)
        s.tx_id,
        s.tx_timestamp,
        s.user_id,
        s.status,
        s.amount                                        as server_amount,
        s.ext_id,

        -- client side (null when dark block)
        c.event_id,
        c.event_timestamp                               as client_timestamp,
        c.intent_value,
        c.product_id,

        -- classification
        case
            when s.ext_id is null and s.status = 'completed' then 'unattributed_server_tx'
            when s.ext_id is null and s.status = 'failed'    then 'failed_unattributed'
            when s.status = 'failed'                          then 'intent_but_failed'
            when s.status = 'completed'                       then 'confirmed_revenue'
        end                                             as reconciliation_status,

        -- price integrity check: should always be 0 for linked records
        s.amount - coalesce(c.intent_value, 0)         as amount_delta,

        -- causal ordering: server must be >= client
        case
            when c.event_timestamp is not null
            then s.tx_timestamp >= c.event_timestamp
            else null
        end                                             as server_after_client,

        -- lag in minutes for linked records
        case
            when c.event_timestamp is not null
            then datediff('minute', c.event_timestamp, s.tx_timestamp)
            else null
        end                                             as lag_minutes,

        date_trunc('day', s.tx_timestamp)              as report_date

    from server s
    left join client_intent c
        on s.ext_id = c.event_id
)

select * from joined
