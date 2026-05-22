"""
Reconaudit Pipeline Report
Generates a self-contained HTML report covering:
  - DLT extraction stats
  - pytest test results
  - dbt test results
  - Reconciliation data with visual breakdown
  - Gemini executive memo

Run:  uv run python report.py
"""

import json
import os
import subprocess
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT   = Path(__file__).parent
DB_PATH     = REPO_ROOT / os.getenv("DUCKDB_PATH", "reconaudit.duckdb")
DBT_BIN     = REPO_ROOT / ".venv/bin/dbt"
PYTEST_BIN  = REPO_ROOT / ".venv/bin/pytest"
MEMO_PATH   = REPO_ROOT / "notes/executive_memo.md"
REPORT_PATH = REPO_ROOT / "notes/report.html"


# ── Data collection ──────────────────────────────────────────────────────────

def collect_dlt_stats() -> dict:
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    raw_ce = conn.execute("SELECT count(*) FROM raw.client_events").fetchone()[0]
    raw_sl = conn.execute("SELECT count(*) FROM raw.server_logs").fetchone()[0]
    by_event = conn.execute(
        "SELECT event_name, count(*) FROM raw.client_events GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    by_status = conn.execute(
        "SELECT status, count(*) FROM raw.server_logs GROUP BY 1 ORDER BY 1"
    ).fetchall()
    conn.close()
    return {
        "client_events": raw_ce,
        "server_logs": raw_sl,
        "by_event": by_event,
        "by_status": by_status,
        "source_client": 5509,
        "source_server": 928,
        "dupes_removed": 5509 - raw_ce,
    }


def run_pytest() -> tuple[int, list[dict]]:
    result = subprocess.run(
        [str(PYTEST_BIN), "tests/", "-v", "--tb=line"],
        cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    tests = []
    # pytest -v writes "path::TestClass::test_name PASSED [ 6%]" to stdout
    for line in result.stdout.splitlines():
        for marker in ("PASSED", "FAILED", "ERROR"):
            if f" {marker}" in line and "::" in line:
                name = line.strip().split("::")[- 1].split(" ")[0]
                tests.append({"name": name, "status": marker})
                break
    return result.returncode, tests


def run_dbt_test() -> tuple[int, list[dict]]:
    env = {**os.environ, "DUCKDB_PATH": f"../{DB_PATH.name}"}
    result = subprocess.run(
        [str(DBT_BIN), "test", "--profiles-dir", ".", "--no-partial-parse"],
        cwd=REPO_ROOT / "transform",
        env=env, capture_output=True, text=True,
    )
    tests = []
    for line in result.stdout.splitlines():
        if " PASS " in line or " FAIL " in line:
            status = "PASS" if " PASS " in line else "FAIL"
            # extract test name from dbt output line
            parts = line.strip().split()
            name_idx = next((i for i, p in enumerate(parts) if p in ("PASS","FAIL")), 1)
            name = parts[name_idx + 1] if name_idx + 1 < len(parts) else "unknown"
            tests.append({"name": name, "status": status})
    return result.returncode, tests


def collect_reconciliation() -> dict:
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    summary = conn.execute("""
        SELECT
            report_date::varchar,
            reconciliation_status,
            tx_count,
            round(server_revenue, 2)          AS server_revenue,
            round(client_declared_revenue, 2) AS client_declared_revenue,
            round(revenue_delta, 2)           AS revenue_delta
        FROM main.fct_revenue_summary
        ORDER BY report_date, reconciliation_status
    """).fetchall()
    top_anomalies = conn.execute("""
        SELECT tx_id, reconciliation_status, round(server_amount, 2),
               round(coalesce(intent_value, 0), 2), round(amount_delta, 2),
               report_date::varchar
        FROM main.fct_reconciliation
        WHERE reconciliation_status != 'confirmed_revenue'
        ORDER BY server_amount DESC
        LIMIT 15
    """).fetchall()
    conn.close()
    return {"summary": summary, "top_anomalies": top_anomalies}


def get_memo() -> str:
    if MEMO_PATH.exists():
        print("  Using cached executive memo.")
        return MEMO_PATH.read_text()
    print("  Running Gemini audit agent...")
    from audit.agent import run_audit
    try:
        memo = run_audit()
        MEMO_PATH.write_text(memo)
        return memo
    except Exception as exc:
        return f"⚠️ Audit agent error: {exc}"


# ── HTML generation ──────────────────────────────────────────────────────────

STATUS_COLORS = {
    "confirmed_revenue":    ("#d4edda", "#155724"),
    "intent_but_failed":    ("#fff3cd", "#856404"),
    "unattributed_server_tx": ("#cce5ff", "#004085"),
    "failed_unattributed":  ("#f8d7da", "#721c24"),
    "TOTAL":                ("#e2e3e5", "#383d41"),
}

STATUS_LABELS = {
    "confirmed_revenue":      "✅ Confirmed Revenue",
    "intent_but_failed":      "⚠️  Intent → Failed",
    "unattributed_server_tx": "🔵 Unattributed Server Tx",
    "failed_unattributed":    "❌ Failed (No Client Event)",
    "TOTAL":                  "📊 TOTAL",
}


def _bar(value: float, max_val: float, color: str) -> str:
    pct = min(100, round(value / max_val * 100)) if max_val else 0
    return (
        f'<div style="background:#e9ecef;border-radius:4px;height:18px;width:200px;display:inline-block;vertical-align:middle">'
        f'<div style="background:{color};height:18px;width:{pct}%;border-radius:4px"></div>'
        f'</div> <span style="font-size:0.85em;color:#555">${value:,.2f}</span>'
    )


def _pill(status: str, label: str) -> str:
    bg, fg = STATUS_COLORS.get(status, ("#6c757d", "#fff"))
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:12px;font-size:0.8em;font-weight:600">{label}</span>'


def generate_html(dlt: dict, py_rc: int, py_tests: list, dbt_rc: int, dbt_tests: list,
                  recon: dict, memo: str, generated_at: str) -> str:

    overall_ok = (py_rc == 0 and dbt_rc == 0)
    banner_color = "#28a745" if overall_ok else "#dc3545"
    banner_text  = "✅ All Systems Green" if overall_ok else "❌ Some Tests Failed"

    # — dbt test rows
    dbt_rows = ""
    for t in dbt_tests:
        ok = t["status"] == "PASS"
        icon = "✅" if ok else "❌"
        dbt_rows += f'<tr><td>{icon}</td><td style="font-family:monospace;font-size:0.82em">{t["name"]}</td></tr>'

    # — pytest rows
    py_rows = ""
    for t in py_tests:
        ok = t["status"] == "PASSED"
        icon = "✅" if ok else "❌"
        short = t["name"]
        py_rows += f'<tr><td>{icon}</td><td style="font-family:monospace;font-size:0.82em">{short}</td></tr>'

    # — reconciliation summary table + chart data
    max_server = max((r[3] for r in recon["summary"] if r[1] == "TOTAL"), default=1)
    recon_rows = ""
    chart_bars = ""
    totals_by_status: dict = defaultdict(lambda: {"count": 0, "server": 0.0, "client": 0.0})

    for row in recon["summary"]:
        date, status, count, server, client, delta = row
        bg, fg = STATUS_COLORS.get(status, ("#fff", "#000"))
        label = STATUS_LABELS.get(status, status)
        delta_str = f'+${delta:,.2f}' if delta >= 0 else f'-${abs(delta):,.2f}'
        recon_rows += (
            f'<tr style="background:{bg};color:{fg}">'
            f'<td>{date[:10]}</td>'
            f'<td>{label}</td>'
            f'<td style="text-align:right">{count}</td>'
            f'<td style="text-align:right">${server:,.2f}</td>'
            f'<td style="text-align:right">${client:,.2f}</td>'
            f'<td style="text-align:right;font-weight:600">{delta_str}</td>'
            f'</tr>'
        )
        if status != "TOTAL":
            totals_by_status[status]["count"] += count
            totals_by_status[status]["server"] += server
            totals_by_status[status]["client"] += client

    # — status summary cards
    cards_html = ""
    for status, vals in sorted(totals_by_status.items()):
        bg, fg = STATUS_COLORS.get(status, ("#fff", "#000"))
        label = STATUS_LABELS.get(status, status)
        cards_html += (
            f'<div style="background:{bg};color:{fg};padding:16px 20px;border-radius:8px;'
            f'min-width:200px;flex:1">'
            f'<div style="font-size:1.4em;font-weight:700">${vals["server"]:,.2f}</div>'
            f'<div style="font-size:0.85em;margin-top:4px">{label}</div>'
            f'<div style="font-size:0.8em;opacity:0.8">{vals["count"]} transactions</div>'
            f'</div>'
        )

    # — top anomalies
    anomaly_rows = ""
    for row in recon["top_anomalies"]:
        tx_id, status, server_amt, client_val, delta, date = row
        bg, fg = STATUS_COLORS.get(status, ("#fff", "#000"))
        label = STATUS_LABELS.get(status, status)
        anomaly_rows += (
            f'<tr>'
            f'<td style="font-family:monospace">{tx_id}</td>'
            f'<td>{_pill(status, label)}</td>'
            f'<td style="text-align:right">${server_amt:,.2f}</td>'
            f'<td style="text-align:right">${client_val:,.2f}</td>'
            f'<td style="text-align:right">${delta:,.2f}</td>'
            f'<td>{date[:10]}</td>'
            f'</tr>'
        )

    # — memo paragraphs
    memo_html = ""
    for para in memo.strip().split("\n\n"):
        if para.startswith("#"):
            level = len(para.split()[0])
            text = para.lstrip("#").strip()
            memo_html += f'<h{level} style="margin-top:1.2em">{text}</h{level}>'
        elif para.startswith("**") or para.startswith("*"):
            memo_html += f'<p style="margin:0.6em 0"><strong>{para.lstrip("*").rstrip("*")}</strong></p>'
        else:
            for line in para.split("\n"):
                if line.strip():
                    memo_html += f'<p style="margin:0.6em 0;line-height:1.7">{line}</p>'

    py_pass = sum(1 for t in py_tests if t["status"] == "PASSED")
    py_total = len(py_tests)
    dbt_pass = sum(1 for t in dbt_tests if t["status"] == "PASS")
    dbt_total = len(dbt_tests)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reconaudit Pipeline Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f5f7fa; color: #1a1a2e; font-size: 15px; }}
  .banner {{ background: {banner_color}; color: white; padding: 18px 40px;
             display: flex; align-items: center; justify-content: space-between; }}
  .banner h1 {{ font-size: 1.4em; font-weight: 700; }}
  .banner .sub {{ font-size: 0.85em; opacity: 0.9; margin-top: 2px; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px; }}
  .section {{ background: white; border-radius: 10px; padding: 24px 28px;
              margin-bottom: 24px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .section h2 {{ font-size: 1.1em; font-weight: 700; margin-bottom: 16px;
                 padding-bottom: 10px; border-bottom: 2px solid #f0f0f0;
                 color: #333; }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
  th {{ background: #f8f9fa; padding: 9px 12px; text-align: left;
        font-weight: 600; color: #555; border-bottom: 2px solid #dee2e6; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #f0f0f0; }}
  tr:last-child td {{ border-bottom: none; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  .stat {{ display: inline-block; background: #f8f9fa; border-radius: 8px;
           padding: 12px 16px; margin: 4px; text-align: center; }}
  .stat .num {{ font-size: 1.6em; font-weight: 700; color: #333; }}
  .stat .lbl {{ font-size: 0.78em; color: #666; margin-top: 2px; }}
  .tag-ok   {{ background:#d4edda; color:#155724; padding:2px 7px; border-radius:4px; font-size:0.8em; }}
  .tag-fail {{ background:#f8d7da; color:#721c24; padding:2px 7px; border-radius:4px; font-size:0.8em; }}
  .memo {{ line-height: 1.75; color: #2c2c2c; }}
  .memo p {{ margin-bottom: 0.8em; }}
  .toc a {{ color: #0066cc; text-decoration: none; margin-right: 16px; font-size: 0.9em; }}
  .toc a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>

<div class="banner">
  <div>
    <div class="h1" style="font-size:1.5em;font-weight:700">{banner_text}</div>
    <div class="sub">Reconaudit Pipeline Report · Generated {generated_at}</div>
  </div>
  <div style="font-size:0.9em;text-align:right">
    <div>pytest: <strong>{py_pass}/{py_total}</strong> passed</div>
    <div>dbt tests: <strong>{dbt_pass}/{dbt_total}</strong> passed</div>
  </div>
</div>

<div class="container">

  <!-- TOC -->
  <div style="margin-bottom:24px" class="toc">
    <a href="#extraction">① Extraction (DLT)</a>
    <a href="#tests">② Tests</a>
    <a href="#reconciliation">③ Reconciliation</a>
    <a href="#memo">④ Executive Memo</a>
  </div>

  <!-- ① EXTRACTION ─────────────────────────────────────────────────── -->
  <div class="section" id="extraction">
    <h2>① Extraction Layer — DLT</h2>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px">
      <div class="stat"><div class="num">{dlt['source_client']:,}</div><div class="lbl">source records<br>(client_events)</div></div>
      <div class="stat"><div class="num" style="color:#28a745">{dlt['client_events']:,}</div><div class="lbl">loaded unique<br>(after dedup)</div></div>
      <div class="stat"><div class="num" style="color:#dc3545">{dlt['dupes_removed']:,}</div><div class="lbl">duplicates<br>removed</div></div>
      <div class="stat"><div class="num">{dlt['source_server']:,}</div><div class="lbl">source records<br>(server_logs)</div></div>
      <div class="stat"><div class="num" style="color:#28a745">{dlt['server_logs']:,}</div><div class="lbl">loaded<br>(no dupes)</div></div>
    </div>
    <div class="two-col">
      <table>
        <thead><tr><th>Event Type</th><th>Count</th></tr></thead>
        <tbody>
          {"".join(f"<tr><td>{r[0]}</td><td>{r[1]:,}</td></tr>" for r in dlt['by_event'])}
        </tbody>
      </table>
      <table>
        <thead><tr><th>Status</th><th>Count</th></tr></thead>
        <tbody>
          {"".join(f"<tr><td>{r[0]}</td><td>{r[1]:,}</td></tr>" for r in dlt['by_status'])}
        </tbody>
      </table>
    </div>
  </div>

  <!-- ② TESTS ─────────────────────────────────────────────────────── -->
  <div class="section" id="tests">
    <h2>② Test Suite</h2>
    <div class="two-col">
      <div>
        <div style="margin-bottom:10px;font-weight:600">
          pytest — {py_pass}/{py_total} passed
          <span class="{"tag-ok" if py_rc==0 else "tag-fail"}" style="margin-left:8px">
            {"ALL PASS" if py_rc==0 else "FAILURES"}
          </span>
        </div>
        <div style="font-size:0.8em;color:#888;margin-bottom:8px">
          Covers: pagination simulation · deduplication · schema evolution
        </div>
        <table>
          <thead><tr><th></th><th>Test</th></tr></thead>
          <tbody>{py_rows}</tbody>
        </table>
      </div>
      <div>
        <div style="margin-bottom:10px;font-weight:600">
          dbt tests — {dbt_pass}/{dbt_total} passed
          <span class="{"tag-ok" if dbt_rc==0 else "tag-fail"}" style="margin-left:8px">
            {"ALL PASS" if dbt_rc==0 else "FAILURES"}
          </span>
        </div>
        <div style="font-size:0.8em;color:#888;margin-bottom:8px">
          Covers: unique · not_null · accepted_values · 3 custom singular tests
        </div>
        <table>
          <thead><tr><th></th><th>Test</th></tr></thead>
          <tbody>{dbt_rows}</tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ③ RECONCILIATION ────────────────────────────────────────────── -->
  <div class="section" id="reconciliation">
    <h2>③ Reconciliation Output</h2>
    <div class="cards">{cards_html}</div>

    <h3 style="font-size:0.95em;font-weight:600;margin:20px 0 10px;color:#555">
      Daily Breakdown by Reconciliation Status
    </h3>
    <table>
      <thead>
        <tr>
          <th>Date</th><th>Status</th><th style="text-align:right">Tx</th>
          <th style="text-align:right">Server $</th>
          <th style="text-align:right">Client Intent $</th>
          <th style="text-align:right">Delta</th>
        </tr>
      </thead>
      <tbody>{recon_rows}</tbody>
    </table>

    <h3 style="font-size:0.95em;font-weight:600;margin:24px 0 10px;color:#555">
      Top Anomalous Transactions (by server amount)
    </h3>
    <table>
      <thead>
        <tr>
          <th>Tx ID</th><th>Status</th>
          <th style="text-align:right">Server $</th>
          <th style="text-align:right">Client $</th>
          <th style="text-align:right">Delta</th>
          <th>Date</th>
        </tr>
      </thead>
      <tbody>{anomaly_rows}</tbody>
    </table>
  </div>

  <!-- ④ EXECUTIVE MEMO ────────────────────────────────────────────── -->
  <div class="section" id="memo">
    <h2>④ Executive Memo — Gemini Audit Agent</h2>
    <div class="memo">{memo_html}</div>
  </div>

</div>
</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("Reconaudit Report Generator")
    print("=" * 40)

    print("→ Collecting DLT stats...")
    dlt_stats = collect_dlt_stats()

    print("→ Running pytest...")
    py_rc, py_tests = run_pytest()
    print(f"  {sum(1 for t in py_tests if t['status']=='PASSED')}/{len(py_tests)} passed")

    print("→ Running dbt test...")
    dbt_rc, dbt_tests = run_dbt_test()
    print(f"  {sum(1 for t in dbt_tests if t['status']=='PASS')}/{len(dbt_tests)} passed")

    print("→ Querying reconciliation data...")
    recon = collect_reconciliation()

    print("→ Fetching executive memo...")
    memo = get_memo()

    print("→ Generating HTML report...")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = generate_html(
        dlt=dlt_stats,
        py_rc=py_rc, py_tests=py_tests,
        dbt_rc=dbt_rc, dbt_tests=dbt_tests,
        recon=recon,
        memo=memo,
        generated_at=generated_at,
    )

    REPORT_PATH.write_text(html, encoding="utf-8")
    print(f"\n✅ Report written to: {REPORT_PATH}")
    print(f"   Opening in browser...")
    webbrowser.open(f"file://{REPORT_PATH}")


if __name__ == "__main__":
    main()
