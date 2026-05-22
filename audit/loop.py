"""
Autonomous audit loop — multi-turn LLM with SQL verification.

Loop structure:
  Turn 1 — Hypothesis: LLM receives reconciliation data and outputs up to
            MAX_FINDINGS findings as a JSON array. Each finding includes a
            verification_sql query it wants run to confirm its claim.
  Turn 2..N — Verify: each SQL is executed against DuckDB (SELECT only).
              The result is fed back to the same conversation; the LLM
              confirms, retracts, or revises the finding based on the data.
  Final turn — Report: executive memo written from confirmed findings only.

This satisfies the brief's "autonomous reasoning" requirement:
  the agent detects a potential bug → the loop runs its own SQL to check
  the raw data → the LLM either confirms or retracts before surfacing to user.

Usage:
    uv run python -m audit.loop
    make audit-loop
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import duckdb
from dotenv import load_dotenv
from openai import OpenAI


@runtime_checkable
class LLMClient(Protocol):
    """
    Minimal interface expected by run_loop().
    The real OpenAI client and MockLLMClient in tests both satisfy this.
    """
    @property
    def chat(self) -> Any: ...

from audit.agent import (
    AUDIT_BASE_URL,
    AUDIT_KEY_ENV,
    AUDIT_MODEL,
    AUDIT_RELEVANT_TAGS,
    DUCKDB_PATH,
    _build_system_prompt,
    _load_decisions,
    _query_summary,
)

load_dotenv()

LOOP_MEMO_PATH = Path("notes/loop_memo.md")
MAX_FINDINGS   = 5   # cap per run — keeps token cost bounded
MAX_SQL_ROWS   = 20  # cap rows per verification result


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_hypothesis_prompt(data: dict) -> str:
    schema_note = (
        "Available DuckDB tables:\n"
        "  main.fct_reconciliation   — 928 rows, one per server transaction\n"
        "  main.fct_revenue_summary  — daily roll-up by reconciliation_status\n"
        "  main.fct_user_journey     — user-day grain with funnel stage flags\n"
        "  raw.client_events         — 4,904 rows (deduped), client event stream\n"
        "  raw.server_logs           — 928 rows, server transaction ledger\n\n"
        "Key columns in fct_reconciliation: tx_id, ext_id, user_id, status, "
        "server_amount, intent_value, amount_delta, reconciliation_status, "
        "server_after_client, lag_minutes, report_date\n"
        "Key columns in fct_user_journey: user_id, activity_date, page_views, "
        "add_to_cart_count, purchase_intent_count, had_page_view, "
        "had_add_to_cart, had_purchase_intent, had_conversion, server_revenue"
    )
    instructions = (
        f"Analyse the reconciliation data below. Identify up to {MAX_FINDINGS} findings.\n\n"
        f"{schema_note}\n\n"
        "Output ONLY a valid JSON array — no prose before or after. "
        "Each element must use this exact schema:\n"
        '[\n'
        '  {\n'
        '    "id": "F1",\n'
        '    "title": "Short title (max 10 words)",\n'
        '    "finding": "Full description of the anomaly or discrepancy",\n'
        '    "category": "overcount|undercount|causality_violation|semantic_drift|attribution_gap",\n'
        '    "estimated_impact_usd": 0.00,\n'
        '    "confidence": "high|medium|low",\n'
        '    "verification_sql": "SELECT ... (must be a SELECT against the tables above)"\n'
        '  }\n'
        ']'
    )
    return (
        f"Here is the reconciliation data:\n\n"
        f"REVENUE SUMMARY:\n{json.dumps(data['revenue_summary'], indent=2)}\n\n"
        f"TOP ANOMALOUS TRANSACTIONS:\n{json.dumps(data['top_anomalies'], indent=2)}\n\n"
        f"{instructions}"
    )


def _build_verification_prompt(finding: dict, ok: bool, result: dict, err: str, row_count: int) -> str:
    result_text = _result_to_text(result) if ok else f"SQL execution failed: {err}"
    fid    = finding.get("id", "?")
    title  = finding.get("title", "Untitled")
    impact = finding.get("estimated_impact_usd", 0.0)
    sql    = finding.get("verification_sql", "(none provided)")
    return (
        f"Finding {fid}: {title}\n"
        f"Your stated finding: {finding.get('finding', '')}\n"
        f"Your estimated impact: ${impact:,.2f}\n\n"
        f"I ran your verification SQL:\n{sql}\n\n"
        f"Result ({row_count} rows returned):\n{result_text}\n\n"
        f"Based on this data, respond with JSON only:\n"
        f'{{"id": "{fid}", "status": "confirmed|retracted|revised", '
        f'"explanation": "one sentence explaining your conclusion", '
        f'"final_impact_usd": 0.00}}'
    )


def _build_memo_prompt(confirmed: list[dict], all_verified: list[dict]) -> str:
    retracted = [v for v in all_verified if v.get("status") != "confirmed"]
    retracted_titles = ", ".join(v.get("title", "?") for v in retracted) or "none"
    return (
        f"The verification loop is complete.\n\n"
        f"Confirmed findings ({len(confirmed)}):\n"
        f"{json.dumps(confirmed, indent=2)}\n\n"
        f"Retracted or unverified ({len(retracted)}): {retracted_titles}\n\n"
        "Write the executive memo now. Base it ONLY on the confirmed findings. "
        "State findings as facts. Name transaction categories, amounts, and dates. "
        "Do not hedge. Do not mention retracted findings."
    )


# ── SQL safety ────────────────────────────────────────────────────────────────

def _safe_execute(sql: str, db_path: str) -> tuple[bool, dict[str, Any], str]:
    """
    Execute sql only if it is a SELECT statement.
    Returns (ok, result_dict, error_msg).
    result_dict has keys 'columns' (list[str]) and 'rows' (list[list]).
    """
    sql = sql.strip()
    if not re.match(r"(?i)^\s*select\b", sql):
        return False, {}, "Rejected — only SELECT queries are permitted in the audit loop"
    conn = None
    try:
        conn = duckdb.connect(db_path, read_only=True)
        rel  = conn.execute(sql)
        cols = [d[0] for d in rel.description]
        rows = [list(r) for r in rel.fetchmany(MAX_SQL_ROWS)]
        return True, {"columns": cols, "rows": rows}, ""
    except Exception as exc:
        return False, {}, str(exc)
    finally:
        if conn:
            conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Any:
    """Extract JSON from LLM output that may include surrounding prose."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    for pattern in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None


def _result_to_text(result: dict[str, Any]) -> str:
    if not result or not result.get("rows"):
        return "(no rows returned)"
    cols  = result["columns"]
    rows  = result["rows"]
    header = " | ".join(str(c) for c in cols)
    sep    = "-" * min(len(header), 120)
    lines  = [header, sep]
    for row in rows:
        lines.append(" | ".join("null" if v is None else str(v)[:60] for v in row))
    return "\n".join(lines)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_loop(
    db_path:       str  = DUCKDB_PATH,
    model:         str  = AUDIT_MODEL,
    base_url:      str  = AUDIT_BASE_URL,
    key_env:       str  = AUDIT_KEY_ENV,
    decision_tags: set  = AUDIT_RELEVANT_TAGS,
    llm_client: LLMClient | None = None,
) -> tuple[str, list[dict]]:
    """
    Run the autonomous audit loop.
    Returns (memo_text, verified_findings_list).

    Pass llm_client to inject a mock (e.g. in tests).  When None, a real
    OpenAI/OpenRouter client is created from env-var credentials.
    Any object passed must satisfy the LLMClient protocol (.chat property).
    """
    if llm_client is not None:
        if not isinstance(llm_client, LLMClient):
            raise TypeError(
                f"llm_client must implement the LLMClient protocol "
                f"(.chat property); got {type(llm_client).__name__}"
            )
        client = llm_client
    else:
        api_key = os.getenv(key_env, "")
        if not api_key or api_key == "your-openrouter-key-here":
            raise ValueError(
                f"{key_env} not set in .env — see .env.example"
            )
        client = OpenAI(base_url=base_url, api_key=api_key)

    decisions  = _load_decisions(tags=decision_tags)
    sys_prompt = _build_system_prompt(decisions)
    data       = _query_summary(db_path)

    # ── Turn 1: Hypothesis generation ────────────────────────────────────────
    print("\n[Loop] Turn 1 — hypothesis generation")
    messages: list[dict] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user",   "content": _build_hypothesis_prompt(data)},
    ]

    resp     = client.chat.completions.create(model=model, messages=messages)
    hyp_text = resp.choices[0].message.content
    messages.append({"role": "assistant", "content": hyp_text})

    findings = _extract_json(hyp_text)
    if not isinstance(findings, list) or not findings:
        print("[Loop] Warning: JSON parse failed — returning raw hypothesis as memo")
        return hyp_text, []

    print(f"[Loop] {len(findings)} finding(s) generated")

    # ── Turn 2..N: Verify each finding ───────────────────────────────────────
    verified: list[dict] = []

    for i, finding in enumerate(findings[:MAX_FINDINGS]):
        fid    = finding.get("id", f"F{i+1}")
        title  = finding.get("title", "Untitled")
        impact = finding.get("estimated_impact_usd", 0.0)
        sql    = finding.get("verification_sql", "")

        print(f"[Loop] Turn {i + 2} — verifying {fid}: {title}")

        ok, result, err = _safe_execute(sql, db_path)
        row_count = len(result.get("rows", [])) if ok else 0

        verify_prompt = _build_verification_prompt(finding, ok, result, err, row_count)
        messages.append({"role": "user", "content": verify_prompt})

        resp         = client.chat.completions.create(model=model, messages=messages)
        verdict_text = resp.choices[0].message.content
        messages.append({"role": "assistant", "content": verdict_text})

        verdict = _extract_json(verdict_text)
        if isinstance(verdict, dict):
            merged = {**finding, **verdict}
        else:
            merged = {
                **finding,
                "status":           "unverified",
                "explanation":      verdict_text[:300],
                "final_impact_usd": impact,
            }

        status = merged.get("status", "unverified")
        final  = merged.get("final_impact_usd", impact)
        print(f"[Loop]   → {status.upper():12s} | ${final:>12,.2f}")
        verified.append(merged)

    # ── Final turn: Write memo ────────────────────────────────────────────────
    confirmed = [v for v in verified if v.get("status") == "confirmed"]
    print(f"\n[Loop] Final turn — writing memo from {len(confirmed)}/{len(verified)} confirmed findings")

    messages.append({"role": "user", "content": _build_memo_prompt(confirmed, verified)})
    resp = client.chat.completions.create(model=model, messages=messages)
    memo = resp.choices[0].message.content

    return memo, verified


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    memo, findings = run_loop()

    LOOP_MEMO_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOOP_MEMO_PATH.write_text(memo, encoding="utf-8")

    confirmed  = [f for f in findings if f.get("status") == "confirmed"]
    retracted  = [f for f in findings if f.get("status") == "retracted"]
    unverified = [f for f in findings if f.get("status") == "unverified"]

    print("\n── Loop Summary " + "─" * 40)
    print(f"  Findings generated : {len(findings)}")
    print(f"  Confirmed          : {len(confirmed)}")
    print(f"  Retracted          : {len(retracted)}")
    print(f"  Unverified         : {len(unverified)}")

    if confirmed:
        print("\n  Confirmed findings:")
        for f in confirmed:
            print(f"    [{f['id']}] {f['title']} — ${f.get('final_impact_usd', 0):,.2f}")

    if retracted:
        print("\n  Retracted (agent self-corrected):")
        for f in retracted:
            print(f"    [{f['id']}] {f['title']} — {f.get('explanation', '')[:80]}")

    print(f"\n  Memo → {LOOP_MEMO_PATH}")
    print("\n" + "─" * 56)
    print(memo)
