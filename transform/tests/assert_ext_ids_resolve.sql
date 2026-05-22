/*
  Singular test: every non-null ext_id in server_logs references a real client event.

  Any row returned here means the server recorded a transaction referencing a
  client event_id that does not exist — a broken foreign key.
  Returns rows that violate this invariant — test passes when 0 rows returned.
*/

select
    tx_id,
    ext_id,
    status,
    server_amount
from {{ ref('fct_reconciliation') }}
where ext_id is not null
  and event_id is null
