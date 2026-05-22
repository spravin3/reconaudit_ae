{{
  config(materialized='view')
}}

/*
  Staging model for client events.

  Deduplication: row_number() over event_id, preferring the most recent DLT load.
  The 605 duplicate event_ids in source are all page_view events; this CTE
  removes them cleanly without touching purchase_intent records.

  Schema evolution: nullable columns for url, traffic_source, product_id, intent_value
  mean new property fields from source land as additional nullable columns without
  breaking downstream models.
*/

with source as (
    select * from {{ source('raw', 'client_events') }}
),

deduped as (
    select
        *,
        row_number() over (
            partition by event_id
            order by _dlt_load_id desc
        ) as _row_num
    from source
)

select
    event_id,
    event_timestamp::timestamp  as event_timestamp,
    user_id,
    event_name,
    url,
    traffic_source,
    product_id,
    intent_value::double        as intent_value
from deduped
where _row_num = 1
