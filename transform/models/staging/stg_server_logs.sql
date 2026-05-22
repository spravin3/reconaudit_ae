{{
  config(materialized='view')
}}

/*
  Staging model for server transaction logs.

  No duplicates exist in source (tx_id is unique), but the deduplication window
  is retained for consistency and to handle future reloads safely.

  ext_id is intentionally preserved as nullable — 225 records in the tx_1703–tx_1927
  block have no client attribution. Downstream models treat this as its own
  reconciliation category ('unattributed_server_tx').
*/

with source as (
    select * from {{ source('raw', 'server_logs') }}
),

deduped as (
    select
        *,
        row_number() over (
            partition by tx_id
            order by _dlt_load_id desc
        ) as _row_num
    from source
)

select
    tx_id,
    tx_timestamp::timestamp  as tx_timestamp,
    user_id,
    status,
    amount::double           as amount,
    ext_id
from deduped
where _row_num = 1
