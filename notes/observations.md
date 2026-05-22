# Data Observations — Phase 0

> Generated: 2026-05-19  
> Sources: `data/client_events.json`, `data/server_logs.json`

---

## 1. Schema Inventory

### `client_events.json` — 5,509 raw records

| Field | Type | Example values |
|---|---|---|
| `event_id` | string (`"a"` + int) | `"a4291"`, `"a515"`, `"a6"` |
| `timestamp` | ISO 8601 UTC string | `"2024-03-01T08:01:00Z"` … `"2024-03-03T08:19:00Z"` |
| `user_id` | string (`"u"` + int) | `"u146"`, `"u183"`, `"u300"` |
| `event_name` | enum | `page_view` (3,605), `add_to_cart` (1,201), `purchase_intent` (703) |
| `properties` | object — varies by event_name | see below |

**Properties schema by event type:**

| `event_name` | Properties fields | Notes |
|---|---|---|
| `page_view` | `url` (string), `source` (string) | urls: `/blog`, `/cart`, `/checkout`, etc. sources: `google`, `facebook`, `affiliate`, `referral`, `tiktok`, `twitter`, `youtube`, `bing`, `email` |
| `add_to_cart` | `product_id` (string) | Format: `"p_101"`, `"p_311"` — no price or quantity |
| `purchase_intent` | `product_id` (string), `value` (float) | value range: **11.61 – 798.19** |

All records have all top-level fields present. No nulls at the top level.

---

### `server_logs.json` — 928 records

| Field | Type | Example values |
|---|---|---|
| `tx_id` | string (`"tx_"` + int) | `"tx_1000"` … `"tx_1927"` |
| `timestamp` | ISO 8601 UTC string | `"2024-03-01T08:08:00Z"` … `"2024-03-03T08:24:00Z"` |
| `user_id` | string | same namespace as client (`u101`–`u300`) |
| `status` | enum | `completed` (786), `failed` (142) |
| `amount` | float | range: **11.61 – 798.19** |
| `meta.ext_id` | string \| null | references `event_id` in client_events, OR `null` (225 records) |

The `meta.ext_id` field is the join key linking server transactions back to client purchase_intent events.

---

## 2. Manually-Spotted Patterns

### 2a. 605 Duplicate `event_id` Values in Client Events
5,509 raw records but only 4,904 unique `event_id` values. The 605 duplicated IDs each appear exactly twice, adding 605 phantom records. All duplicates are **`page_view` events** — zero duplicates exist in `purchase_intent` or `add_to_cart`. Every duplicate pair is byte-for-byte identical (same timestamp, user_id, and properties). These inflate session counts and funnel top-of-funnel metrics, but do not directly double-count revenue.

### 2b. Structural Split in Server `tx_id` Numbering — The Null-Ext-Id Block
The 928 server transactions form two structurally distinct sub-populations:

| Range | Count | `ext_id` present? | Interpretation |
|---|---|---|---|
| `tx_1000` – `tx_1702` | 703 | Yes (all) | Every tx has a linked client event |
| `tx_1703` – `tx_1927` | 225 | No (all null) | No client linkage whatsoever |

This is not random missingness — it is a hard boundary. The null block represents **225 transactions with $71,698.73 in volume** (176 completed = $56,768.99; 49 failed = $14,929.74) that are completely invisible to any analytics report built on the client event stream. The most plausible explanation is a client SDK instrumentation failure or pipeline break during a specific window that cut off `ext_id` population.

### 2c. 93 Failed Transactions With Matching Client Purchase Intent
Of the 703 client `purchase_intent` events (each one linked 1:1 to a server transaction via `ext_id`), **93 server transactions returned `status: failed`**, totaling **$39,162.34**. For all 93, the `amount` equals the client `value` exactly. The client side has no `status` field — it recorded the intent but has no mechanism to know the payment failed. If any reporting pipeline treats `purchase_intent` value as confirmed revenue, these 93 events produce a systematic overcount.

### 2d. Amount–Value Exact Match Across All 703 Linked Records
For every `(server_log, client_event)` pair joined on `ext_id = event_id`, `amount == value` to the cent. There are **zero price-level discrepancies** between what the client logged and what the server processed for any individual transaction. The discrepancy is structural (what events exist and their statuses), not a rounding or pricing error.

### 2e. Amount Range Coincidence
Server `amount` range (11.61 – 798.19) is identical to client `purchase_intent.value` range. This confirms the two fields are derived from the same pricing source, not independently calculated.

### 2f. No Duplicate `tx_id` Values in Server Logs
Server log is clean — no transaction appears twice.

### 2g. No Backward Timestamp Causality
For all 703 linked pairs, `server.timestamp >= client.timestamp`. No server transaction precedes its client event. The maximum server lag is under 1 hour for all records.

### 2h. 176 "Ghost" Completed Transactions ($56,768.99)
The null-ext-id completed transactions (176 records) represent **real, settled revenue the server confirms but the client cannot attribute to any user journey**. On March 2 alone these total $27,062.02.

---

## 3. Daily Revenue Comparison

| Date | Client `purchase_intent` total | Server completed (attributed) | Server completed (unattributed) | Gap (intent – attr) |
|---|---|---|---|---|
| 2024-03-01 | $80,141.11 | $64,949.33 | $18,433.38 | **$15,191.78** |
| 2024-03-02 | $147,674.13 | $128,095.51 | $27,062.02 | **$19,578.62** |
| 2024-03-03 | $48,118.58 | $43,726.64 | $11,273.59 | **$4,391.94** |
| **Total** | **$275,933.82** | **$236,771.48** | **$56,768.99** | **$39,162.34** |

The $39,162.34 total gap equals the sum of all 93 failed attributed transactions — confirming the gap is entirely explained by purchase_intent events whose server transaction failed.

---

## 4. Specific Candidate Anomalies

1. **`tx_1392` / `a2578` — the $196.99 phantom**  
   User u182, March 3. Client records `purchase_intent` for $196.99. Server transaction returns `failed`. If a revenue dashboard sums purchase_intent values without filtering on server confirmation, this $196.99 is overcounted. This is the single closest individual record to the $200 discrepancy figure.

2. **`tx_1909` (null ext_id, $378.32, completed) — user u201's ghost transaction**  
   User u201 has two properly attributed completed transactions AND this third completed transaction that has no client event. The client analytics pipeline would show u201 with $1,089.91 in revenue; the server shows $1,468.23. The delta ($378.32) is invisible to attribution.

3. **The `tx_1703` boundary — a hard cutoff, not random noise**  
   Every transaction from `tx_1703` onward has `ext_id: null`. This is almost certainly a single instrumentation event (deployment, config change, SDK drop) rather than scattered data loss. The boundary coincides with March 1–2 overlap. This is the highest-leverage anomaly in the dataset.

4. **`tx_1675` / `a4721` — $170.19 failed, user u231**  
   Client records purchase_intent; server fails the payment. Same pattern as #1 but for a higher-volume user (u231 appears in other page_view events).

5. **`tx_1246` / `a1562` — $690.63 failed**  
   One of the largest failed-attributed transactions. A $690.63 purchase_intent appeared on the client, but the server rejected the payment. Any revenue report not joining server status would overstate by $690.63 for this single record.

6. **605 duplicate page_view events — conversion rate inflation**  
   All 605 duplicates are page_view events. Funnel analyses built on client events would see inflated page_view volumes (~24% more top-of-funnel events than reality), artificially depressing reported conversion rates (fewer "purchases per view"). This is a data quality issue that corrupts attribution and funnel metrics even if it does not affect revenue totals directly.

7. **u243 — 6 completed attributed transactions ($2,706.83)**  
   Highest completed-transaction count for a single user. All 6 are properly linked with matching purchase_intent events. Worth flagging as a power user or potential test account.

8. **49 failed + null ext_id transactions ($14,929.74)**  
   Server-side failures with no client event at all. These represent payment attempts the client never logged — possibly server-initiated retries, backend payment flows, or third-party-triggered charges that bypassed the client SDK entirely.

9. **March 2 concentration — 53% of all purchase_intent volume in one day**  
   $147,674.13 of the 3-day $275,933.82 total falls on March 2. The largest single-day revenue gap also lands on March 2 ($19,578.62). Any incident investigation should start there.

10. **No `add_to_cart` events ever link to server transactions**  
    The join key (`ext_id = event_id`) only matches `purchase_intent` events, never `add_to_cart`. This confirms `add_to_cart` is a mid-funnel signal only and should not be aggregated with revenue metrics — but its presence in the event stream means care is needed in any `GROUP BY event_name` revenue query.

---

## 5. Revenue Summary Table

| Category | Count | Total Amount |
|---|---|---|
| Client purchase_intent values | 703 | $275,933.82 |
| Server completed (attributed) | 610 | $236,771.48 |
| Server completed (unattributed, null ext_id) | 176 | $56,768.99 |
| Server failed (attributed — client showed intent) | 93 | $39,162.34 |
| Server failed (unattributed, null ext_id) | 49 | $14,929.74 |
| **Server total completed** | **786** | **$293,540.47** |

---

## 6. Hypothesis: Where the $200 Discrepancy Comes From

The $200 discrepancy is most likely a symptom of a single failed transaction (`tx_1392`, $196.99) surfacing in a client-side revenue report that does not validate against server `status`. The mechanism: the client's analytics pipeline sums `purchase_intent.value` as a proxy for revenue. When a dashboard operator compares a filtered cohort (e.g., "revenue for user u182 on March 3") against the finance team's server-confirmed total, they see $196.99 on one side and $0.00 on the other — close enough to the stated $200 to anchor the investigation.

The deeper story, however, is structural: the $200 is a visible symptom of two overlapping system-level problems:

1. **Client overcounts via unvalidated intent**: 93 `purchase_intent` events totaling $39,162.34 in value were never settled on the server. Any client-side revenue metric that does not join on `status = completed` is systematically overstated.

2. **Server undercounts in attribution**: 176 completed transactions ($56,768.99) have no client event, making them invisible to any dashboard built on the client stream. This creates an equal-and-opposite undercounting in client-attributed revenue.

These two forces partially cancel each other in aggregate ($56,768.99 − $39,162.34 = $17,606.65 net server-side surplus), but at the per-user, per-day, or per-product level they diverge in unpredictable ways — exactly the kind of discrepancy that manifests as a $200 anomaly in one report slice and a $20,000 anomaly in another.

The most defensible hypothesis for the executive memo: **the $200 figure traces to a failed payment that the client system counted as confirmed revenue and the server did not**. The systemic fix requires joining every client `purchase_intent` to `server_logs` on `ext_id`, filtering to `status = completed`, and treating unattributed server completions as a separate reconciliation line item.
