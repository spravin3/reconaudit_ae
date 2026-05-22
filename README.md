# Reconciliation & Audit Engine

A Principal Analytics Engineer take-home assessment. Extracts, reconciles, and audits revenue data from a client-side event stream and a server-side transaction ledger — using DLT, dbt, DuckDB, and a raw LLM audit loop with no high-level frameworks.

---

## Stakeholder Briefing Memo

**To:** The CEO
**Re:** The $200 Revenue Discrepancy — What It Is and What We Found Behind It
**Date:** March 2024

The $200 gap is real, traceable to a specific failed payment, and is a visible sign of two measurement problems our audit has now fully quantified.

**What caused the $200**

Our marketing dashboard records revenue the moment a customer signals intent to buy. Our payment system records revenue only when money actually clears. One transaction — a $196.99 purchase — was logged as revenue on the marketing side because the customer clicked through. The payment processor rejected it. No money moved. One system said yes, while the other said no, causing the $200 discrepancy.

**The larger finding**

This pattern is not isolated. Our audit of 928 transactions over three days found two problems running in opposite directions:

- **$39,162.34** across 93 transactions where customers signalled intent but payment failed. The marketing dashboard overcounts by this amount.
- **$56,768.99** across 176 completed payments that settled in full but generated no marketing event. Real money arrived — the marketing system never recorded it.

These two errors run in opposite directions and partially offset each other at the aggregate level — a $56,768 blind spot on one side, a $39,162 overcount on the other, netting to a $17,606 gap in favour of the server ledger. The $200 is not the net of these two — it is one specific failed transaction that illustrates the pattern.



**Which number to trust**

For revenue reporting, board numbers, and finance: the payment transaction ledger is the only authoritative record. It is the only system that confirms money changed hands.

For marketing performance, channel ROI, and funnel analysis: the customer event stream is the right source — but only for journey metrics, never for revenue totals.

**Path forward**

The reconciliation engine built for this audit flags every gap between the two systems automatically, by category and dollar amount, on every pipeline run. The structural fix is instrumenting our tracking layer so that every confirmed payment generates a matching customer event — closing the attribution gap at the source rather than measuring it after the fact.

---

## Architecture & Data Flow

```
client_events.json          server_logs.json
       │                           │
       └──────────┬────────────────┘
                  ▼
         DLT extraction layer
         · Pagination simulation (CHUNK_SIZE=500)
         · Primary-key deduplication (merge)
         · Column type hints (schema stability)
                  │
                  ▼
       DuckDB — raw schema
       raw.client_events (4,904 rows after dedup)
       raw.server_logs   (928 rows, clean)
                  │
                  ▼
         dbt — staging layer (views)
         · Type casting and column rename
         · Second dedup pass (ROW_NUMBER)
         · Strip DLT metadata columns
                  │
                  ▼
         dbt — intermediate layer (views)
         · int_events_enriched       ← events + funnel metadata + channel classification
         · int_user_transaction_summary ← per-user server aggregates
                  │
                  ▼
         dbt — marts layer (tables)
         ┌─────────────────────────────────────────────────┐
         │  DIMS                    │  FACTS                │
         │  dim_users               │  fct_reconciliation   │
         │  dim_products            │  fct_user_journey     │
         │  dim_dates               │  fct_revenue_summary  │
         └─────────────────────────────────────────────────┘
                  │
                  ▼
         Autonomous LLM audit loop  (audit/loop.py)
         · Turn 1: LLM generates findings as JSON with verification SQL
         · Turn 2–N: loop runs each SQL against DuckDB, feeds result back
         · LLM confirms, revises, or retracts each finding from the data
         · Final turn: memo written from confirmed findings only
                  │
                  ▼
         HTML report (report.py)
         · Extraction stats · Test results · Reconciliation tables · Memo
         · Opens in browser automatically
```

**Two sources of truth — by design**

| Purpose | Source | Why |
|---|---|---|
| Financial reporting | Server transaction ledger | Only system that records payment cleared or failed |
| Marketing attribution | Client event stream | Only system that records channel, funnel stage, and user journey |
| Reconciliation gap | `fct_reconciliation` | Bridge that makes the divergence visible and measurable |

---

## How to Run

**Prerequisites:** Python 3.11+, `uv`, an OpenRouter API key in `.env` (see `.env.example`).

```bash
# Install dependencies
make install

# Full pipeline: extract → transform → test → audit → open HTML report
make all

# Individual steps
make pipeline       # DLT extraction only
make transform      # dbt models only
make test           # pytest (15 pipeline tests) + dbt tests
make audit          # single-turn LLM audit → notes/executive_memo.md
make audit-loop     # autonomous multi-turn loop → notes/loop_memo.md
make report         # HTML report → opens in browser
make docs           # dbt DAG lineage → opens at localhost:8080
```

**Switching LLM provider** (no code change required):

```bash
# OpenRouter → Claude
AUDIT_MODEL=anthropic/claude-3.5-haiku make audit-loop

# Anthropic native
AUDIT_BASE_URL=https://api.anthropic.com/v1 AUDIT_KEY_ENV=ANTHROPIC_API_KEY AUDIT_MODEL=claude-3-5-haiku-20241022 make audit-loop
```

---

## Test Suite

```bash
# All 38 tests (pipeline + audit loop)
make test-py

# Audit loop tests only — no API key needed, runs in 0.5s
uv run pytest tests/test_audit_loop.py -v
```

**Pipeline tests (15):** pagination boundary correctness, DLT deduplication, schema evolution resilience.

**Audit loop tests (23):** SQL safety gating (SELECT-only), JSON extraction from prose/fenced LLM output, all 7 data scenarios with mock LLM clients, structural guarantees (malformed hypothesis, DDL injection, zero confirmed findings still produces a memo).

The retraction test is the key one: `test_s3a_causality_retracted_when_zero_violations` verifies that when the loop's verification SQL returns zero rows, the LLM retracts its own finding before it reaches the output memo.

---

## Philosophical Trade-offs

These decisions are documented in full in `DECISIONS.md` (D-01 through D-10), which is also injected into the LLM audit agent as architectural context so it does not flag intentional design choices as data anomalies.

| Decision | Chose | Over | Because |
|---|---|---|---|
| Extraction framework | DLT lightweight | Plain `json.load` + INSERT | Production patterns (merge, schema hints, pagination) without framework overhead |
| Storage | DuckDB single file | Postgres / Parquet | Zero-setup, portable, reviewable — correct scope for a 6-hour assessment |
| Deduplication | Two layers (DLT merge + dbt ROW_NUMBER) | One layer | DLT handles cross-run duplicates; dbt handles intra-batch duplicates |
| Schema evolution | Explicit column mapping | `**props` passthrough | Controlled schema growth — new upstream fields require a deliberate code change, not a silent column addition |
| Join direction | Server LEFT JOIN client | Client LEFT JOIN or INNER | Every server transaction must appear in reconciliation — including the 225-record dark block ($56,768.99) |
| Reconciliation taxonomy | 4 categories | Binary matched/unmatched | Each category has a distinct owner and remediation path |
| LLM agent | Raw OpenAI SDK + autonomous loop | LangChain / CrewAI | Brief explicitly forbids high-level frameworks; raw SDK is more transparent and testable |
| Model routing | OpenRouter | Native provider SDKs | One API key, model-switchable via env var, no per-provider account setup |

---

## Repository Structure

```
reconaudit_ae/
├── data/                        # Source JSON files (client_events, server_logs)
├── pipeline/
│   ├── sources.py               # DLT resource definitions
│   └── run.py                   # Pipeline entrypoint
├── transform/
│   └── models/
│       ├── staging/             # stg_client_events, stg_server_logs
│       ├── intermediate/        # int_events_enriched, int_user_transaction_summary
│       └── marts/               # fct_reconciliation, fct_user_journey, fct_revenue_summary
│                                  dim_users, dim_products, dim_dates
├── audit/
│   ├── agent.py                 # Single-turn LLM audit agent
│   └── loop.py                  # Autonomous multi-turn loop with SQL verification
├── tests/
│   ├── test_pipeline.py         # 15 pipeline tests
│   ├── test_audit_loop.py       # 23 audit loop tests
│   └── scenarios.md             # All 7 test scenarios documented
├── notes/
│   ├── observations.md          # Phase 0 data analysis
│   ├── executive_memo.md        # AI-generated: single-turn agent output
│   └── loop_memo.md             # AI-generated: autonomous loop output
├── DECISIONS.md                 # Append-only architectural decision log
├── report.py                    # Self-contained HTML report generator
├── Makefile                     # All pipeline commands
└── .env.example                 # Environment variable reference
```

---

## A Note on the Memos

`notes/executive_memo.md` and `notes/loop_memo.md` are generated by the LLM audit agent and autonomous loop respectively — they are evidence of the agentic QA system working. The **Stakeholder Briefing Memo** at the top of this README is written by the engineer (pravin here) and represents the communication exercise: translating the same findings into language a CEO can act on.
