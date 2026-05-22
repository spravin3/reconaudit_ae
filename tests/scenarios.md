# Audit Loop Scenarios

Reference file for manually testing the autonomous audit loop against known
data facts. Each scenario maps to a finding the loop should generate, the SQL
it should propose, and the conclusion it should reach after verification.

Run the loop:  `make audit-loop`
Loop output:   `notes/loop_memo.md`
Single-turn:   `notes/executive_memo.md`

---

## Scenario 1 — The $200 Trigger (intent_but_failed)

**Hypothesis the loop should generate:**
The CEO's $200 discrepancy traces to a specific failed transaction where the
client recorded purchase_intent but the server returned failed. Any dashboard
summing client intent_value without joining server status will overcount.

**SQL the loop should propose (or equivalent):**
```sql
SELECT tx_id, ext_id, user_id, status,
       ROUND(server_amount, 2)   AS server_amount,
       ROUND(intent_value, 2)    AS intent_value,
       reconciliation_status,
       report_date::varchar
FROM main.fct_reconciliation
WHERE reconciliation_status = 'intent_but_failed'
ORDER BY server_amount DESC
LIMIT 10;
```

**Expected result shape:**
- 93 total rows in this category
- `tx_1392` / `a2578` / `$196.99` appears near the top — closest individual record to the stated $200
- All rows show `status = failed` with a non-null `intent_value`
- `amount_delta` equals `server_amount` for all rows (server charged $0, client declared the full amount)

**Expected loop conclusion:** CONFIRMED
The loop should confirm the overcount mechanism and name tx_1392 specifically.
Total category impact: $39,162.34 across 93 transactions.

---

## Scenario 2 — The Dark Block Hard Boundary (unattributed_server_tx)

**Hypothesis the loop should generate:**
225 server transactions (tx_1703 through tx_1927) have no corresponding client
event — not because of random missingness but because of a hard sequential
cutoff. This is a single instrumentation event, not scattered data loss.

**SQL the loop should propose (or equivalent):**
```sql
SELECT tx_id, ext_id, status,
       ROUND(server_amount, 2) AS server_amount,
       reconciliation_status
FROM main.fct_reconciliation
WHERE tx_id IN (
    'tx_1700','tx_1701','tx_1702',
    'tx_1703','tx_1704','tx_1705'
)
ORDER BY tx_id;
```

**Expected result shape:**
- tx_1700, tx_1701, tx_1702 have non-null ext_id → confirmed_revenue
- tx_1703, tx_1704, tx_1705 have null ext_id → unattributed_server_tx
- The boundary is exact: no interleaving, no exceptions

**Expected loop conclusion:** CONFIRMED
Hard sequential boundary confirmed. 176 completed unattributed transactions
totalling $56,768.99 in settled revenue invisible to the client stream.

**Why this is the highest-leverage finding:**
This is not a client overcounting error. This is real settled revenue that the
attribution system cannot see. Unlike the overcount (which cancels some of the
undercount), this is a pure blind spot in the analytics infrastructure.

---

## Scenario 3 — Agent Self-Validation: Causality Check

**Hypothesis the loop should generate:**
For all linked transaction pairs, the server timestamp is greater than or equal
to the client timestamp — no server transaction precedes its client event.

**SQL the loop should propose (or equivalent):**
```sql
SELECT COUNT(*) AS causality_violations
FROM main.fct_reconciliation
WHERE server_after_client = false;
```

**Expected result shape:**
- Single row: `causality_violations = 0`

**Expected loop conclusion:** CONFIRMED (no violations found)
This is the scenario where the loop validates a negative — the absence of
violations is itself a data quality signal. The loop should confirm that
causal ordering holds across all 703 linked pairs, and NOT flag this as a bug.

**This tests the "retraction" path indirectly:**
If the loop had hypothesised a causality violation and then run this query, the
0-count result would force it to RETRACT. That retraction mechanism is the core
of the autonomous reasoning requirement.

---

## Scenario 4 — Semantic Drift: Conversion Without Client Intent

**Hypothesis the loop should generate:**
Users show server-side completed transactions on days where no client-side
purchase_intent event was fired. This is the "purchase without preceding session
activity" pattern the brief describes as semantic drift.

**SQL the loop should propose (or equivalent):**
```sql
SELECT user_id, activity_date,
       had_page_view, had_add_to_cart,
       had_purchase_intent, had_conversion,
       ROUND(server_revenue, 2) AS server_revenue
FROM main.fct_user_journey
WHERE had_conversion = true
  AND had_purchase_intent = false
ORDER BY server_revenue DESC
LIMIT 10;
```

**Expected result shape:**
- Rows where `had_conversion = true` and `had_purchase_intent = false`
- These are exactly the 225 dark-block records expressed at user-day grain
- `had_page_view` will also be false for most of these rows

**Expected loop conclusion:** CONFIRMED
Semantic drift confirmed: server-completed transactions with zero client-side
intent signal on the same day. These users are attribution-dark — they appear
in server revenue but are invisible to any funnel or attribution report.

---

## Scenario 5 — Net Financial Impact: Overcount vs Undercount

**Hypothesis the loop should generate:**
The two primary error types (client overcount via failed intents, and server
undercount via missing attribution) partially offset each other in aggregate,
which masks the true scale of the infrastructure problem.

**SQL the loop should propose (or equivalent):**
```sql
SELECT
    SUM(CASE WHEN reconciliation_status = 'intent_but_failed'
             THEN intent_value ELSE 0 END)       AS client_overcount,
    SUM(CASE WHEN reconciliation_status = 'unattributed_server_tx'
             THEN server_amount ELSE 0 END)      AS server_undercount,
    SUM(CASE WHEN reconciliation_status = 'unattributed_server_tx'
             THEN server_amount ELSE 0 END)
  - SUM(CASE WHEN reconciliation_status = 'intent_but_failed'
             THEN intent_value ELSE 0 END)       AS net_server_surplus
FROM main.fct_reconciliation;
```

**Expected result shape:**

| client_overcount | server_undercount | net_server_surplus |
|---|---|---|
| 39,162.34 | 56,768.99 | 17,606.65 |

**Expected loop conclusion:** CONFIRMED
The overcount ($39,162.34) and undercount ($56,768.99) do not cancel.
The server shows $17,606.65 more confirmed revenue than the client attributes.
At aggregate level this looks manageable; at per-user or per-day level the
divergence is unpredictable — which is what produces the $200 figure in one
reporting slice.

---

## Scenario 6 — Ghost Transaction: Per-User Discrepancy (u201)

**Hypothesis the loop should generate:**
Individual users have a measurable gap between client-declared revenue and
server-confirmed revenue, caused by the attribution-dark transactions. User
u201 is a concrete example: server confirms more revenue than the client logged.

**SQL the loop should propose (or equivalent):**
```sql
SELECT
    user_id,
    ROUND(SUM(CASE WHEN reconciliation_status = 'confirmed_revenue'
                   THEN server_amount ELSE 0 END), 2)  AS server_confirmed,
    ROUND(SUM(CASE WHEN intent_value IS NOT NULL
                   THEN intent_value ELSE 0 END), 2)   AS client_declared,
    ROUND(SUM(CASE WHEN reconciliation_status = 'unattributed_server_tx'
                   THEN server_amount ELSE 0 END), 2)  AS ghost_revenue
FROM main.fct_reconciliation
WHERE user_id = 'u201'
GROUP BY user_id;
```

**Expected result shape:**
- u201 has a non-zero `ghost_revenue` (~$378.32)
- `server_confirmed` > `client_declared` by that ghost amount
- Any LTV or revenue per-user metric built on client data understates u201 by $378.32

**Expected loop conclusion:** CONFIRMED
Per-user ghost revenue confirmed. This pattern affects all 225 users with
unattributed transactions and is systemic, not an isolated anomaly.

---

## Scenario 7 — False Positive Guard: DECISIONS.md Context Test

**What this scenario tests:**
Without architectural context injected, the loop would flag pipeline
implementation choices as data bugs. This scenario tests that the DECISIONS.md
guard works — the loop should NOT flag these as findings.

**Things the loop must NOT flag as bugs (they are intentional):**

| Pattern visible in data | Why it is NOT a bug |
|---|---|
| 605 fewer client_events in raw vs source | D-02: DLT merge deduped exact duplicates |
| 928 rows in fct_reconciliation, 225 with null client columns | D-05: server-left join is intentional |
| 4 reconciliation_status values instead of just matched/unmatched | D-06: 4-category taxonomy is intentional |
| _dlt_* columns in raw.client_events | D-01: DLT metadata, stripped in staging |

**SQL to verify the dedup was intentional (not a data loss):**
```sql
SELECT
    (SELECT COUNT(*) FROM raw.client_events)                AS loaded_count,
    5509 - (SELECT COUNT(*) FROM raw.client_events)         AS removed_count,
    'D-02: byte-identical page_view duplicates removed'     AS reason;
```

**Expected result shape:**
- `loaded_count = 4904`
- `removed_count = 605`
- The loop should NOT flag this as missing data; it should recognise it as deduplication

**Expected loop conclusion:** The loop should produce zero findings related to
these patterns. If the loop incorrectly flags any of them, it means the
DECISIONS.md context injection is not working as the validation gate.

**How to test the guard explicitly:**
Temporarily comment out `_load_decisions()` in `audit/loop.py` so `decisions = ""`
and rerun. The loop will generate false positives about deduplication and join
direction. Re-enable it — those false positives should disappear. The delta
between the two runs is the value of the context injection mechanism.

---

## Running All Scenarios Against the Loop

```bash
# Run the autonomous loop (writes notes/loop_memo.md)
make audit-loop

# Compare single-turn vs loop output
diff notes/executive_memo.md notes/loop_memo.md

# Open DuckDB directly to verify SQL from any scenario
duckdb reconaudit.duckdb
```

**What to look for in the loop output (`notes/loop_memo.md`):**
1. Loop summary shows confirmed vs retracted count
2. Scenario 3 (causality) should appear as confirmed with 0 violations
3. Any finding that was retracted means the loop self-corrected — that is the
   autonomous reasoning working as designed
4. The memo at the end should reference only confirmed findings by name and amount
