/*
  Singular test: server transaction must never precede its client purchase_intent.

  A server tx timestamped before the client event that triggered it is physically
  impossible and indicates either clock skew, data fabrication, or an ETL bug.
  Returns rows that violate this invariant — test passes when 0 rows returned.
*/

select
    tx_id,
    ext_id,
    client_timestamp,
    tx_timestamp,
    lag_minutes
from {{ ref('fct_reconciliation') }}
where server_after_client = false
