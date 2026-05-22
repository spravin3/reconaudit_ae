{{
  config(materialized='table')
}}

/*
  Daily revenue summary by reconciliation status.

  This is the model an executive dashboard or audit agent would query first.
  Each row is one (date, status) combination showing client-declared intent vs
  server-confirmed amount and the resulting delta.

  Key reading:
    - confirmed_revenue rows: client and server agree — delta should be 0
    - intent_but_failed rows: client_revenue > 0, server_revenue = 0 — overcount risk
    - unattributed_server_tx: server_revenue > 0, client_revenue = 0 — undercount risk
    - failed_unattributed: both 0, but tx attempted — operational noise
*/

with recon as (
    select * from {{ ref('fct_reconciliation') }}
),

daily_by_status as (
    select
        report_date,
        reconciliation_status,
        count(*)                            as tx_count,
        sum(server_amount)                  as server_revenue,
        sum(coalesce(intent_value, 0))      as client_declared_revenue,
        sum(server_amount)
            - sum(coalesce(intent_value, 0)) as revenue_delta
    from recon
    group by 1, 2
),

totals as (
    select
        report_date,
        'TOTAL'                             as reconciliation_status,
        count(*)                            as tx_count,
        sum(server_amount)                  as server_revenue,
        sum(coalesce(intent_value, 0))      as client_declared_revenue,
        sum(server_amount)
            - sum(coalesce(intent_value, 0)) as revenue_delta
    from recon
    group by 1
)

select * from daily_by_status
union all
select * from totals
order by report_date, reconciliation_status
