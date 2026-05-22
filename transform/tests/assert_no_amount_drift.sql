/*
  Singular test: no price-level discrepancy between linked client and server records.

  For every confirmed_revenue row (server completed + client intent matched),
  server_amount must equal client intent_value to the cent.
  Returns rows that violate this invariant — test passes when 0 rows returned.
*/

select
    tx_id,
    ext_id,
    server_amount,
    intent_value,
    amount_delta
from {{ ref('fct_reconciliation') }}
where reconciliation_status = 'confirmed_revenue'
  and abs(amount_delta) > 0.01
