"""
LLM-powered audit agent via OpenRouter (raw openai SDK, no frameworks).

OpenRouter gives a single API key that routes to any model — Gemini, Claude, etc.
We default to google/gemini-2.0-flash-001 to stay true to the original spec intent,
but any OpenRouter model slug works via the AUDIT_MODEL env var.

Context injection: DECISIONS.md is loaded and injected into the system prompt so the
LLM understands architectural choices before interpreting the data. Tag-filtered so
only [MODEL] and [AUDIT] entries are sent (keeps tokens lean as the file grows).

Usage:
    uv run python -m audit.agent

API key added in .env files
"""

import json
import os
import re
from pathlib import Path

import duckdb
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ── Provider config — all overridable via .env ───────────────────────────────
# Switch provider examples (no code change needed):
#   OpenRouter → Gemini:   AUDIT_MODEL=google/gemini-2.0-flash-001  (default)
#   OpenRouter → Claude:   AUDIT_MODEL=anthropic/claude-3.5-haiku
#   Anthropic native:      AUDIT_BASE_URL=https://api.anthropic.com/v1
#                          AUDIT_KEY_ENV=ANTHROPIC_API_KEY
#                          AUDIT_MODEL=claude-3-5-haiku-20241022
#   OpenAI native:         AUDIT_BASE_URL=https://api.openai.com/v1
#                          AUDIT_KEY_ENV=OPENAI_API_KEY
#                          AUDIT_MODEL=gpt-4o-mini
AUDIT_BASE_URL  = os.getenv("AUDIT_BASE_URL", "https://openrouter.ai/api/v1")
AUDIT_KEY_ENV   = os.getenv("AUDIT_KEY_ENV",  "OPENROUTER_API_KEY")
AUDIT_MODEL     = os.getenv("AUDIT_MODEL",    "google/gemini-2.0-flash-001")
DUCKDB_PATH     = os.getenv("DUCKDB_PATH",    "reconaudit.duckdb")
DECISIONS_PATH  = Path("DECISIONS.md")
MEMO_PATH       = Path("notes/executive_memo.md")

# Tags to inject for the audit agent. [EXTRACT] entries describe the pipeline
# internals — not useful context for a revenue memo. [MODEL] and [AUDIT] entries
# explain the business logic and taxonomy the LLM needs to reason correctly.
AUDIT_RELEVANT_TAGS = {"[MODEL]", "[AUDIT]", "[INFRA]"}

SYSTEM_PROMPT = """
You are a senior data integrity auditor reviewing a 3-day revenue reconciliation
between two systems: a client-side event stream and a server-side transaction ledger.

Your task:
1. Identify the root causes of revenue discrepancies between the two systems.
2. Classify each discrepancy type (overcount, undercount, attribution gap, etc.).
3. Estimate the dollar impact per category.
4. Write a concise executive memo (5–8 paragraphs) that a CFO can act on.

Be specific: name transaction categories, amounts, and dates.
Do not hedge. State findings as facts supported by the data you are given.
Do not flag pipeline implementation choices (deduplication, join direction, etc.)
as data quality issues — those are intentional architectural decisions documented
in the context you have been given.
""".strip()


# ── DECISIONS.md context loader ───────────────────────────────────────────────

def _load_decisions(
    path: Path = DECISIONS_PATH,
    tags: set[str] = AUDIT_RELEVANT_TAGS,
) -> str:
    """
    Load DECISIONS.md and return only entries whose tag matches `tags`.

    Each entry starts with a heading like:
        ## D-05 · [MODEL] Join direction: ...

    Filtering by tag keeps the injected context lean as the file grows.
    When the file has <50 entries, passing tags=None loads everything.
    """
    if not path.exists():
        return ""

    text = path.read_text(encoding="utf-8")

    if tags is None:
        return text

    # Split on H2 headings (each decision block)
    blocks = re.split(r"\n(?=## D-)", text)
    header = blocks[0]  # file title + preamble

    relevant = [
        block for block in blocks[1:]
        if any(tag in block.split("\n")[0] for tag in tags)
    ]

    if not relevant:
        return ""

    return header.strip() + "\n\n" + "\n\n---\n\n".join(relevant)


# ── Data retrieval ────────────────────────────────────────────────────────────

def _query_summary(db_path: str) -> dict:
    conn = duckdb.connect(db_path, read_only=True)

    summary_rows = conn.execute("""
        select
            report_date::varchar,
            reconciliation_status,
            tx_count,
            round(server_revenue, 2)          as server_revenue,
            round(client_declared_revenue, 2) as client_declared_revenue,
            round(revenue_delta, 2)           as revenue_delta
        from main.fct_revenue_summary
        order by report_date, reconciliation_status
    """).fetchall()

    anomaly_rows = conn.execute("""
        select
            tx_id, ext_id, status,
            round(server_amount, 2)                  as server_amount,
            round(coalesce(intent_value, 0), 2)      as intent_value,
            round(amount_delta, 2)                   as amount_delta,
            reconciliation_status,
            report_date::varchar
        from main.fct_reconciliation
        where reconciliation_status != 'confirmed_revenue'
        order by server_amount desc
        limit 25
    """).fetchall()

    conn.close()

    cols_summary = [
        "report_date", "reconciliation_status", "tx_count",
        "server_revenue", "client_declared_revenue", "revenue_delta",
    ]
    cols_anomaly = [
        "tx_id", "ext_id", "status", "server_amount", "intent_value",
        "amount_delta", "reconciliation_status", "report_date",
    ]
    return {
        "revenue_summary": [dict(zip(cols_summary, r)) for r in summary_rows],
        "top_anomalies":   [dict(zip(cols_anomaly,  r)) for r in anomaly_rows],
    }


# ── Prompt construction ───────────────────────────────────────────────────────

def _build_system_prompt(decisions_context: str) -> str:
    if not decisions_context:
        return SYSTEM_PROMPT

    return (
        SYSTEM_PROMPT
        + "\n\n"
        + "─" * 60
        + "\n"
        + "ARCHITECTURAL CONTEXT (from DECISIONS.md):\n"
        + "The following decisions explain how this data was modelled.\n"
        + "Use this to distinguish intentional design from data anomalies.\n\n"
        + decisions_context
    )


def _build_user_prompt(data: dict) -> str:
    return f"""
Here is the daily revenue reconciliation data:

REVENUE SUMMARY (by date and status):
{json.dumps(data["revenue_summary"], indent=2)}

TOP ANOMALOUS TRANSACTIONS (non-confirmed-revenue, highest server amount first):
{json.dumps(data["top_anomalies"], indent=2)}

Reconciliation status definitions:
- confirmed_revenue        : server completed + matching client purchase_intent
- intent_but_failed        : client fired purchase_intent, server transaction failed
- unattributed_server_tx   : server completed, no client event (attribution gap)
- failed_unattributed      : server failed, no client event

Write the executive memo now.
""".strip()


# ── OpenRouter call ───────────────────────────────────────────────────────────

def run_audit(
    db_path: str        = DUCKDB_PATH,
    model: str          = AUDIT_MODEL,
    base_url: str       = AUDIT_BASE_URL,
    key_env: str        = AUDIT_KEY_ENV,
    decision_tags: set  = AUDIT_RELEVANT_TAGS,
) -> str:
    api_key = os.getenv(key_env, "")
    if not api_key or api_key == "your-openrouter-key-here":
        raise ValueError(
            f"{key_env} not set in .env. "
            "See .env.example for provider setup options."
        )

    client = OpenAI(base_url=base_url, api_key=api_key)

    decisions  = _load_decisions(tags=decision_tags)
    sys_prompt = _build_system_prompt(decisions)
    data       = _query_summary(db_path)
    user_prompt = _build_user_prompt(data)

    token_estimate = (len(sys_prompt) + len(user_prompt)) // 4
    print(f"  Context: {len(decisions.splitlines())} decision lines injected "
          f"(~{token_estimate:,} tokens total)")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": user_prompt},
        ],
    )
    return response.choices[0].message.content


if __name__ == "__main__":
    memo = run_audit()
    print(memo)
    MEMO_PATH.write_text(memo, encoding="utf-8")
    print(f"\nMemo written to {MEMO_PATH}")
