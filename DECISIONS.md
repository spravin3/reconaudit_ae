# DECISIONS.md
> Append-only log. One entry per architectural or analytical choice.
> Format: Decision → Alternatives → Chose because → Trade-off.
> Tag each entry: [EXTRACT] [TRANSFORM] [MODEL] [AUDIT] [INFRA]
> The audit agent loads this file as context before every LLM call.

---

## D-01 · [EXTRACT] Extraction layer: DLT over plain Python reads

Decision: Use DLT as the extraction framework instead of loading JSON directly into DuckDB with raw Python.

Alternatives considered:
- Plain `json.load()` + `duckdb.execute("INSERT ...")` — fewer moving parts
- DLT full pipeline mode (with normaliser + staging tables) — more powerful

Chose DLT lightweight because:
The source data already showed 605 duplicate event_ids and a need to handle three
different properties shapes per event type. DLT gives primary_key-based deduplication
on every reload, explicit column type hints for schema stability, and pagination
simulation without writing it from scratch. Lightweight mode avoids over-engineering
for two JSON files while still demonstrating production ELT patterns.

Trade-off accepted:
DLT creates internal _dlt_* metadata tables in the database. Any direct DuckDB query
must filter or ignore those tables. The staging models handle this transparently.

---

## D-02 · [EXTRACT] Deduplication strategy: write_disposition=merge + primary_key

Decision: Set write_disposition="merge" and declare primary_key on both DLT resources
(event_id for client_events, tx_id for server_logs).

Alternatives considered:
- write_disposition="append" + deduplicate in dbt staging with ROW_NUMBER()
- write_disposition="replace" (full reload every run)

Chose merge because:
Observations confirmed 605 byte-for-byte identical page_view duplicates in source.
Deduplicating at DLT load time means staging models stay clean. The ROW_NUMBER()
dedup window is retained in stg_client_events as a defensive second layer. Replace
would be correct for small datasets but breaks incremental loads.

Trade-off accepted:
Merge is slower than append on large datasets. Acceptable at this scale (<10k rows).

---

## D-03 · [EXTRACT] Pagination simulation: CHUNK_SIZE = 500

Decision: Yield records in batches of 500 to simulate real API pagination behaviour.

Alternatives considered:
- CHUNK_SIZE=100 (more pages, tighter rate-limit simulation)
- CHUNK_SIZE=1000
- Stream line-by-line from file

Chose 500 because:
500 is a realistic default page size for most commerce and analytics REST APIs
(Shopify, Segment, Mixpanel all use 100–500). The goal is to demonstrate the pattern
and verify no records drop at page boundaries — not to optimise for these flat files.
The pagination test suite validates boundary correctness regardless of chunk size.

Trade-off accepted:
The chunk size is arbitrary here — not derived from an actual API pagination contract.

---

## D-04 · [EXTRACT] Properties flattening: sparse columns over nested JSON

Decision: Promote nested `properties` to top-level columns (url, traffic_source,
product_id, intent_value), with NULL for fields that don't apply to a given event type.

Alternatives considered:
- Store properties as DuckDB STRUCT or JSON blob, unnest at query time
- Separate tables per event type (one for page_view, one for purchase_intent)

Chose sparse columns because:
dbt handles nullable columns cleanly. Downstream reconciliation models only ever touch
purchase_intent columns. Keeping everything in one table makes the staging model
self-documenting. Schema evolution (a new property field in source) just adds a
nullable column — no migration needed.

Trade-off accepted:
The table carries sparse columns (url/traffic_source null for 1,904 non-page_view rows;
intent_value null for 4,201 non-purchase_intent rows). Acceptable at this scale.

---

## D-05 · [MODEL] Join direction: server LEFT JOIN client

Decision: In fct_reconciliation, drive the join from server_logs LEFT JOIN client_events
on ext_id = event_id.

Alternatives considered:
- client LEFT JOIN server — client as driving table
- INNER JOIN — only matched pairs
- FULL OUTER JOIN — everything on both sides

Chose server-left because:
The server transaction ledger is the financial record of truth. Every server transaction
must appear in the reconciliation output — including the 225-record dark block
(tx_1703–tx_1927) with no client event. An inner join silently drops those 225 rows
and $56,768.99 in completed revenue. A client-left join misses unattributed server
completions entirely.

Trade-off accepted:
225 rows have null client columns. Every downstream query that aggregates client-side
revenue must use COALESCE(intent_value, 0) or it returns incorrect totals.

---

## D-06 · [MODEL] Reconciliation taxonomy: 4 categories

Decision: Classify every server transaction into one of four reconciliation statuses:
confirmed_revenue, intent_but_failed, unattributed_server_tx, failed_unattributed.

Alternatives considered:
- Binary: matched / unmatched
- Six categories (split by amount range, split unattributed by date)
- Status driven by server.status only, no client join

Chose 4 categories because:
The four categories map directly to the four root causes identified in observations:
(1) clean revenue, (2) client overcounts via unvalidated intent, (3) server undercounts
via attribution loss, (4) fully dark failures. Each has a distinct remediation owner.
Fewer categories merge problems that require different teams to fix.

Trade-off accepted:
intent_but_failed conflates payment failures, network retries, and cancellations.
A production system needs a sub-status field to separate these causes.

---

## D-07 · [TRANSFORM] Model architecture: two layers (staging + marts)

Decision: Two dbt layers — staging (views, dedup + cast) and marts (tables, business logic).

Alternatives considered:
- Single model that does everything
- Three layers with an intermediate metrics layer
- Full Medallion (bronze/silver/gold)

Chose two layers because:
With two source tables and one reconciliation question, a full Medallion is premature.
Staging handles DLT-specific noise (_dlt_* columns, nullable types, duplicates).
Marts handle business logic. This boundary matters architecturally: it keeps
transformation concerns separate from modelling concerns, which matters when the
source schema evolves.

Trade-off accepted:
Staging adds a layer of indirection for a transformation that could fit in one SQL file.
Worth it for demonstrating the right pattern even at small scale.

---

## D-08 · [INFRA] Storage: DuckDB single-file database

Decision: Store all raw and transformed data in a single DuckDB file (reconaudit.duckdb).

Alternatives considered:
- PostgreSQL (Dockerised)
- Parquet files on local filesystem
- In-memory DuckDB (no persistence between runs)

Chose DuckDB file because:
The pipeline runs locally with no server process. dbt-duckdb points directly at the
file. The file is the single artefact a reviewer can open and query without any setup.
For a 6-hour assessment with no infra requirement, this is the correct scope choice.

Trade-off accepted:
DuckDB file is not safe for concurrent writes. The pipeline (DLT) and transform (dbt)
must run sequentially — which they do, via make all.

---

## D-09 · [INFRA] LLM provider: OpenRouter with openai SDK

Decision: Route all LLM calls through OpenRouter's API using the openai Python package.

Alternatives considered:
- google-generativeai SDK → Gemini native API (original spec)
- anthropic SDK → Claude native
- Raw HTTP with requests/httpx

Chose OpenRouter because:
The google-generativeai SDK is deprecated and the free tier hit daily quota limits
immediately. OpenRouter provides one API key that routes to Gemini, Claude, or any
other model with no per-provider account setup. The openai SDK is a typed HTTP client,
not a framework. Switching models is a one-line .env change (AUDIT_MODEL).

Trade-off accepted:
OpenRouter adds one network hop (~50–100ms) and requires trusting a third-party router.
Acceptable for an assessment; a production system would use provider SDKs directly.

---

## D-10 · [AUDIT] Agent context: DECISIONS.md injected into LLM system prompt

Decision: The audit agent reads DECISIONS.md and injects it as architectural context
into the system prompt before sending reconciliation data to the LLM.

Alternatives considered:
- No context injection (agent sees data only)
- Inject observations.md instead
- Tag-filtered injection (only [MODEL] and [AUDIT] entries)

Chose full DECISIONS.md injection because:
Without architectural context the LLM flags intentional design choices as anomalies.
It would not know that 605 duplicate page_views were removed by DLT design (D-02),
or that the 225-record dark block is a known structural split (D-05), not an ETL bug.
The decisions log gives the agent the "why" layer so its findings address business
data, not pipeline implementation.

Scaling note: when DECISIONS.md exceeds ~3,000 tokens, switch to tag-filtered injection
(load only [MODEL] and [AUDIT] tagged entries for the audit agent, [EXTRACT] entries
for a pipeline health agent). See D-10 implementation in audit/agent.py.

Trade-off accepted:
~2,000 tokens of additional context per call. Negligible at current pricing.
At 50+ entries the file should be summarised or tag-filtered before injection.
