-- Per-user server transaction aggregates.
-- Shared input for dim_users and fct_user_journey to avoid repeating the same subquery.
with txns as (
    select * from {{ ref('stg_server_logs') }}
)

select
    user_id,
    count(*)                                                          as total_txns,
    count(*) filter (where status = 'completed')                      as completed_txns,
    count(*) filter (where status = 'failed')                         as failed_txns,
    round(sum(amount) filter (where status = 'completed'), 2)         as total_revenue,
    round(avg(amount), 2)                                             as avg_tx_amount,
    min(tx_timestamp)                                                 as first_tx_at,
    max(tx_timestamp)                                                 as last_tx_at
from txns
group by user_id
