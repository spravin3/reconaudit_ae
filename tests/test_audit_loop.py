"""
Tests for the autonomous audit loop (audit/loop.py).

Tier 1 — Unit tests (no LLM, lightweight DuckDB):
  TestSafeSQLExecution   — SQL safety gating: SELECT allowed, DDL/DML rejected
  TestExtractJSON        — JSON extraction from raw, prose-wrapped, and fenced LLM output

Tier 2 — Loop mechanics (mock LLM + scenario DuckDB via make_scenario_db fixture):
  TestLoopMechanics      — 8 scenario tests verifying confirmed/retracted behaviour

Each TestLoopMechanics test:
  1. Creates a scenario-specific DuckDB with known synthetic rows.
  2. Provides a MockLLMClient with pre-written Turn-1 (hypothesis JSON),
     Turn-2..N (verification verdict JSON), and final (memo text) responses.
  3. Calls run_loop() with the mock injected.
  4. Asserts the returned verified_findings match expected status and impact.

The MockLLMClient fully replaces the real OpenAI/OpenRouter call, making all
loop-mechanics tests deterministic, instant, and free.
"""

import json
from pathlib import Path

import duckdb
import pytest

from audit.loop import (
    MAX_SQL_ROWS,
    _extract_json,
    _result_to_text,
    _safe_execute,
    run_loop,
)


# ── Mock LLM client ───────────────────────────────────────────────────────────

class _FakeMessage:
    def __init__(self, content: str):
        self.content = content

class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)

class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]

class _FakeCompletions:
    def __init__(self, responses: list[str]):
        self._responses = responses
        self._idx = 0

    def create(self, **kwargs) -> _FakeResponse:
        content = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return _FakeResponse(content)

class _FakeChat:
    def __init__(self, responses: list[str]):
        self.completions = _FakeCompletions(responses)

class MockLLMClient:
    """
    Deterministic stand-in for the OpenAI client used by run_loop().

    Responses are consumed in order. If the loop makes more calls than
    there are responses, the last response is repeated.
    """
    def __init__(self, responses: list[str]):
        self.chat = _FakeChat(responses)


# ── Helpers for building mock responses ──────────────────────────────────────

def _hypothesis(findings: list[dict]) -> str:
    """Serialize a findings list as the JSON the mock returns on Turn 1."""
    return json.dumps(findings)


def _verdict(fid: str, status: str, explanation: str, impact: float) -> str:
    """Serialize a single finding verdict as the mock returns on Turn 2..N."""
    return json.dumps({
        "id": fid,
        "status": status,
        "explanation": explanation,
        "final_impact_usd": impact,
    })


# ── Scenario 1: intent_but_failed rows ───────────────────────────────────────

_S1_INSERT = [
    """
    INSERT INTO fct_revenue_summary VALUES
        ('2024-03-03', 'intent_but_failed',   93,      0.00, 39162.34, -39162.34),
        ('2024-03-03', 'confirmed_revenue',  610, 236771.48, 236771.48,     0.00)
    """,
    """
    INSERT INTO fct_reconciliation VALUES
        ('tx_1392','2024-03-03 10:00:00','u182','failed',196.99,'a2578','a2578',
         '2024-03-03 09:55:00',196.99,'p_456','intent_but_failed',196.99,true,5,'2024-03-03'),
        ('tx_1250','2024-03-01 10:00:00','u100','failed',500.00,'a1000','a1000',
         '2024-03-01 09:55:00',500.00,'p_100','intent_but_failed',500.00,true,5,'2024-03-01')
    """,
]

_S1_VERIFICATION_SQL = (
    "SELECT tx_id, status, ROUND(server_amount,2), reconciliation_status "
    "FROM main.fct_reconciliation "
    "WHERE reconciliation_status = 'intent_but_failed' ORDER BY server_amount DESC"
)

# ── Scenario 2: dark block hard boundary ─────────────────────────────────────

_S2_INSERT = [
    """
    INSERT INTO fct_revenue_summary VALUES
        ('2024-03-02', 'unattributed_server_tx', 176, 56768.99, 0.00, 56768.99),
        ('2024-03-02', 'confirmed_revenue',      610, 236771.48, 236771.48, 0.00)
    """,
    """
    INSERT INTO fct_reconciliation VALUES
        ('tx_1700','2024-03-01 10:00:00','u100','completed',250.00,'a1500','a1500',
         '2024-03-01 09:55:00',250.00,'p_200','confirmed_revenue',0.00,true,5,'2024-03-01'),
        ('tx_1701','2024-03-01 10:05:00','u101','completed',300.00,'a1501','a1501',
         '2024-03-01 10:00:00',300.00,'p_201','confirmed_revenue',0.00,true,5,'2024-03-01'),
        ('tx_1702','2024-03-01 10:10:00','u102','completed',150.00,'a1502','a1502',
         '2024-03-01 10:05:00',150.00,'p_202','confirmed_revenue',0.00,true,5,'2024-03-01'),
        ('tx_1703','2024-03-02 08:00:00','u200','completed',378.32,NULL,NULL,
         NULL,NULL,NULL,'unattributed_server_tx',378.32,NULL,NULL,'2024-03-02'),
        ('tx_1704','2024-03-02 08:05:00','u201','completed',450.00,NULL,NULL,
         NULL,NULL,NULL,'unattributed_server_tx',450.00,NULL,NULL,'2024-03-02'),
        ('tx_1705','2024-03-02 08:10:00','u202','failed',200.00,NULL,NULL,
         NULL,NULL,NULL,'failed_unattributed',200.00,NULL,NULL,'2024-03-02')
    """,
]

_S2_VERIFICATION_SQL = (
    "SELECT tx_id, ext_id, reconciliation_status "
    "FROM main.fct_reconciliation "
    "WHERE tx_id IN ('tx_1700','tx_1701','tx_1702','tx_1703','tx_1704','tx_1705') "
    "ORDER BY tx_id"
)

# ── Scenario 3a: causality — no violations (loop should RETRACT) ──────────────

_S3A_INSERT = [
    """
    INSERT INTO fct_revenue_summary VALUES
        ('2024-03-01', 'confirmed_revenue', 703, 236771.48, 236771.48, 0.00)
    """,
    """
    INSERT INTO fct_reconciliation VALUES
        ('tx_1000','2024-03-01 10:00:00','u100','completed',100.00,'a100','a100',
         '2024-03-01 09:55:00',100.00,'p_1','confirmed_revenue',0.00,true,5,'2024-03-01'),
        ('tx_1001','2024-03-01 10:05:00','u101','completed',200.00,'a101','a101',
         '2024-03-01 10:00:00',200.00,'p_2','confirmed_revenue',0.00,true,5,'2024-03-01')
    """,
]

_S3A_VERIFICATION_SQL = (
    "SELECT COUNT(*) AS causality_violations "
    "FROM main.fct_reconciliation WHERE server_after_client = false"
)

# ── Scenario 3b: causality — one violation present (loop should CONFIRM) ─────

_S3B_INSERT = [
    """
    INSERT INTO fct_revenue_summary VALUES
        ('2024-03-01', 'confirmed_revenue', 703, 236771.48, 236771.48, 0.00)
    """,
    """
    INSERT INTO fct_reconciliation VALUES
        ('tx_1000','2024-03-01 09:50:00','u100','completed',100.00,'a100','a100',
         '2024-03-01 09:55:00',100.00,'p_1','confirmed_revenue',0.00,false,-5,'2024-03-01')
    """,
]

# ── Scenario 4: semantic drift — conversion with no purchase_intent ───────────

_S4_INSERT = [
    """
    INSERT INTO fct_revenue_summary VALUES
        ('2024-03-02', 'unattributed_server_tx', 225, 71698.73, 0.00, 71698.73)
    """,
    """
    INSERT INTO fct_reconciliation VALUES
        ('tx_1703','2024-03-02 08:00:00','u200','completed',378.32,NULL,NULL,
         NULL,NULL,NULL,'unattributed_server_tx',378.32,NULL,NULL,'2024-03-02')
    """,
    """
    INSERT INTO fct_user_journey VALUES
        ('u200','2024-03-02',0,0,0,0,0,0.0,NULL,NULL,false,1,1,0,378.32,false,false,false,true),
        ('u100','2024-03-01',5,2,2,1,1,100.00,'google','organic',false,1,1,0,100.00,true,true,true,true)
    """,
]

_S4_VERIFICATION_SQL = (
    "SELECT user_id, activity_date, had_purchase_intent, had_conversion, "
    "ROUND(server_revenue,2) AS server_revenue "
    "FROM main.fct_user_journey "
    "WHERE had_conversion = true AND had_purchase_intent = false"
)

# ── Scenario 5: net financial impact ─────────────────────────────────────────

_S5_INSERT = [
    """
    INSERT INTO fct_revenue_summary VALUES
        ('2024-03-01','intent_but_failed',   2,    0.00,  300.00, -300.00),
        ('2024-03-02','unattributed_server_tx',3, 600.00,    0.00,  600.00)
    """,
    """
    INSERT INTO fct_reconciliation VALUES
        ('tx_A','2024-03-01 10:00:00','u100','failed',100.00,'a100','a100',
         '2024-03-01 09:55:00',100.00,'p_1','intent_but_failed',100.00,true,5,'2024-03-01'),
        ('tx_B','2024-03-01 11:00:00','u101','failed',200.00,'a101','a101',
         '2024-03-01 10:55:00',200.00,'p_2','intent_but_failed',200.00,true,5,'2024-03-01'),
        ('tx_C','2024-03-02 08:00:00','u200','completed',150.00,NULL,NULL,
         NULL,NULL,NULL,'unattributed_server_tx',150.00,NULL,NULL,'2024-03-02'),
        ('tx_D','2024-03-02 08:05:00','u201','completed',250.00,NULL,NULL,
         NULL,NULL,NULL,'unattributed_server_tx',250.00,NULL,NULL,'2024-03-02'),
        ('tx_E','2024-03-02 08:10:00','u202','completed',200.00,NULL,NULL,
         NULL,NULL,NULL,'unattributed_server_tx',200.00,NULL,NULL,'2024-03-02')
    """,
]

_S5_VERIFICATION_SQL = (
    "SELECT "
    "ROUND(SUM(CASE WHEN reconciliation_status='intent_but_failed' THEN intent_value ELSE 0 END),2) AS client_overcount, "
    "ROUND(SUM(CASE WHEN reconciliation_status='unattributed_server_tx' THEN server_amount ELSE 0 END),2) AS server_undercount "
    "FROM main.fct_reconciliation"
)

# ── Scenario 6: ghost transaction for u201 ────────────────────────────────────

_S6_INSERT = [
    """
    INSERT INTO fct_revenue_summary VALUES
        ('2024-03-02','unattributed_server_tx',1,378.32,0.00,378.32),
        ('2024-03-02','confirmed_revenue',     2,500.00,500.00,0.00)
    """,
    """
    INSERT INTO fct_reconciliation VALUES
        ('tx_1100','2024-03-01 10:00:00','u201','completed',200.00,'a200','a200',
         '2024-03-01 09:55:00',200.00,'p_1','confirmed_revenue',0.00,true,5,'2024-03-01'),
        ('tx_1200','2024-03-01 11:00:00','u201','completed',300.00,'a201','a201',
         '2024-03-01 10:55:00',300.00,'p_2','confirmed_revenue',0.00,true,5,'2024-03-01'),
        ('tx_1703','2024-03-02 08:00:00','u201','completed',378.32,NULL,NULL,
         NULL,NULL,NULL,'unattributed_server_tx',378.32,NULL,NULL,'2024-03-02')
    """,
]

_S6_VERIFICATION_SQL = (
    "SELECT user_id, "
    "ROUND(SUM(CASE WHEN reconciliation_status='confirmed_revenue' THEN server_amount ELSE 0 END),2) AS server_confirmed, "
    "ROUND(SUM(CASE WHEN reconciliation_status='unattributed_server_tx' THEN server_amount ELSE 0 END),2) AS ghost_revenue "
    "FROM main.fct_reconciliation WHERE user_id='u201' GROUP BY user_id"
)


# ═════════════════════════════════════════════════════════════════════════════
# Tier 1 — SQL safety and JSON extraction
# ═════════════════════════════════════════════════════════════════════════════

class TestSafeSQLExecution:
    """_safe_execute() must allow SELECT and block everything else."""

    def _make_db(self, tmp_path, extra_sqls=None):
        db = str(tmp_path / "unit.duckdb")
        conn = duckdb.connect(db)
        conn.execute(
            "CREATE TABLE t (id INTEGER, val DOUBLE, label VARCHAR)"
        )
        conn.execute("INSERT INTO t VALUES (1, 10.5, 'alpha'), (2, 20.0, 'beta')")
        for sql in (extra_sqls or []):
            conn.execute(sql)
        conn.close()
        return db

    def test_valid_select_returns_data(self, tmp_path):
        db = self._make_db(tmp_path)
        ok, result, err = _safe_execute("SELECT id, val FROM t ORDER BY id", db)
        assert ok, f"Expected ok=True, got err={err}"
        assert result["columns"] == ["id", "val"]
        assert result["rows"] == [[1, 10.5], [2, 20.0]]

    def test_delete_is_rejected(self, tmp_path):
        db = self._make_db(tmp_path)
        ok, result, err = _safe_execute("DELETE FROM t WHERE id = 1", db)
        assert not ok
        assert "SELECT" in err or "Rejected" in err

    def test_drop_is_rejected(self, tmp_path):
        db = self._make_db(tmp_path)
        ok, result, err = _safe_execute("DROP TABLE t", db)
        assert not ok
        assert "SELECT" in err or "Rejected" in err

    def test_invalid_sql_returns_error_tuple(self, tmp_path):
        db = self._make_db(tmp_path)
        ok, result, err = _safe_execute("SELECT * FROM nonexistent_table_xyz", db)
        assert not ok
        assert err != ""
        assert result == {}

    def test_rows_capped_at_max_sql_rows(self, tmp_path):
        db = self._make_db(tmp_path)
        # Insert enough rows to exceed MAX_SQL_ROWS
        conn = duckdb.connect(db)
        for i in range(3, MAX_SQL_ROWS + 10):
            conn.execute(f"INSERT INTO t VALUES ({i}, {i * 1.0}, 'row{i}')")
        conn.close()
        ok, result, err = _safe_execute("SELECT * FROM t", db)
        assert ok
        assert len(result["rows"]) <= MAX_SQL_ROWS

    def test_select_with_expression_no_table(self, tmp_path):
        db = self._make_db(tmp_path)
        ok, result, err = _safe_execute("SELECT 1 + 1 AS answer", db)
        assert ok
        assert result["rows"] == [[2]]


class TestExtractJSON:
    """_extract_json() must handle the variety of formats LLMs return."""

    def test_pure_json_array(self):
        raw = '[{"id": "F1", "status": "confirmed"}]'
        result = _extract_json(raw)
        assert isinstance(result, list)
        assert result[0]["id"] == "F1"

    def test_pure_json_object(self):
        raw = '{"id": "F1", "status": "retracted", "final_impact_usd": 0.0}'
        result = _extract_json(raw)
        assert isinstance(result, dict)
        assert result["status"] == "retracted"

    def test_json_embedded_in_prose(self):
        raw = (
            "Based on the data, here is my analysis:\n\n"
            '[{"id":"F1","finding":"Revenue gap","estimated_impact_usd":39162.34}]\n\n'
            "I hope that helps."
        )
        result = _extract_json(raw)
        assert isinstance(result, list)
        assert result[0]["id"] == "F1"

    def test_json_in_markdown_code_fence(self):
        raw = '```json\n[{"id": "F2", "status": "confirmed"}]\n```'
        result = _extract_json(raw)
        assert isinstance(result, list)
        assert result[0]["status"] == "confirmed"

    def test_completely_non_json_returns_none(self):
        raw = "I cannot determine any findings from this data."
        result = _extract_json(raw)
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# Tier 2 — Loop mechanics with mock LLM
# ═════════════════════════════════════════════════════════════════════════════

class TestLoopMechanics:
    """
    Each test verifies one scenario from tests/scenarios.md.

    The MockLLMClient returns pre-written responses so the tests are
    deterministic and make no real LLM calls.  The scenario DB has exactly
    the rows needed so the verification SQL returns meaningful results.

    Loop call count per test:
      Turn 1 = 1 (hypothesis)
      Turn 2 = 1 (verify finding F1)
      Final  = 1 (memo)
      Total  = 3 responses needed for a single-finding scenario
    """

    # ── Scenario 1: intent_but_failed confirmed ───────────────────────────────

    def test_s1_intent_but_failed_confirmed(self, make_scenario_db):
        db = make_scenario_db(_S1_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "93 failed intents overcount client revenue",
            "finding": (
                "93 server transactions returned failed but client recorded "
                "purchase_intent, producing $39,162.34 in phantom client revenue."
            ),
            "category": "overcount",
            "estimated_impact_usd": 39162.34,
            "confidence": "high",
            "verification_sql": _S1_VERIFICATION_SQL,
        }])
        verdict = _verdict("F1", "confirmed",
                           "2 intent_but_failed rows confirmed including tx_1392/$196.99",
                           39162.34)
        memo = "Confirmed: 93 failed intents overcount client revenue by $39,162.34."

        mock = MockLLMClient([hypothesis, verdict, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        confirmed = [f for f in findings if f.get("status") == "confirmed"]
        assert len(confirmed) == 1, f"Expected 1 confirmed finding, got {confirmed}"
        assert confirmed[0]["final_impact_usd"] == pytest.approx(39162.34)
        assert confirmed[0]["category"] == "overcount"

    # ── Scenario 2: dark block boundary confirmed ─────────────────────────────

    def test_s2_dark_block_confirmed(self, make_scenario_db):
        db = make_scenario_db(_S2_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "Hard null boundary at tx_1703 — 225 unattributed txns",
            "finding": (
                "Server transactions tx_1703 onwards all have null ext_id. "
                "This is a sequential cutoff indicating an instrumentation failure, "
                "not random missingness. 176 completed transactions total $56,768.99."
            ),
            "category": "attribution_gap",
            "estimated_impact_usd": 56768.99,
            "confidence": "high",
            "verification_sql": _S2_VERIFICATION_SQL,
        }])
        verdict = _verdict("F1", "confirmed",
                           "tx_1700-1702 have ext_id; tx_1703-1705 are null — hard boundary confirmed",
                           56768.99)
        memo = "Confirmed: $56,768.99 in settled revenue is permanently attribution-dark."

        mock = MockLLMClient([hypothesis, verdict, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        confirmed = [f for f in findings if f.get("status") == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["category"] == "attribution_gap"
        assert confirmed[0]["final_impact_usd"] == pytest.approx(56768.99)

    # ── Scenario 3a: causality retracted when zero violations ─────────────────

    def test_s3a_causality_retracted_when_zero_violations(self, make_scenario_db):
        """
        The loop hypothesises a causality violation.
        The verification SQL returns COUNT=0.
        The loop RETRACTS the finding.
        This is the core autonomous reasoning test.
        """
        db = make_scenario_db(_S3A_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "Possible server-before-client causality violations",
            "finding": "Some server transactions may precede their client events.",
            "category": "causality_violation",
            "estimated_impact_usd": 0.00,
            "confidence": "medium",
            "verification_sql": _S3A_VERIFICATION_SQL,
        }])
        # After seeing COUNT=0, the LLM retracts
        retraction = _verdict("F1", "retracted",
                              "Zero causality violations found. All server timestamps follow client.",
                              0.00)
        memo = "No confirmed findings. Causal ordering holds across all linked pairs."

        mock = MockLLMClient([hypothesis, retraction, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        retracted = [f for f in findings if f.get("status") == "retracted"]
        confirmed = [f for f in findings if f.get("status") == "confirmed"]
        assert len(retracted) == 1, "Expected loop to self-retract the causality finding"
        assert len(confirmed) == 0

    # ── Scenario 3b: causality confirmed when violation exists ────────────────

    def test_s3b_causality_confirmed_when_violation_exists(self, make_scenario_db):
        """
        Inverse of 3a: the DB has a real violation.
        Verification SQL returns COUNT=1, loop confirms.
        """
        db = make_scenario_db(_S3B_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "Server-before-client causality violation detected",
            "finding": "At least one server transaction precedes its client event.",
            "category": "causality_violation",
            "estimated_impact_usd": 0.00,
            "confidence": "medium",
            "verification_sql": _S3A_VERIFICATION_SQL,
        }])
        confirmation = _verdict("F1", "confirmed",
                                "1 row returned with server_after_client=false. Violation confirmed.",
                                0.00)
        memo = "Confirmed: 1 causality violation found — tx_1000 precedes its client event."

        mock = MockLLMClient([hypothesis, confirmation, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        confirmed = [f for f in findings if f.get("status") == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["category"] == "causality_violation"

    # ── Scenario 4: semantic drift confirmed ──────────────────────────────────

    def test_s4_semantic_drift_confirmed(self, make_scenario_db):
        """
        Server-completed transactions where the client never fired purchase_intent.
        The brief's example: 'a Purchase without a preceding Session Start.'
        """
        db = make_scenario_db(_S4_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "225 server conversions have no client purchase_intent",
            "finding": (
                "fct_user_journey has rows where had_conversion=true and "
                "had_purchase_intent=false. Server settled revenue that the "
                "client analytics stack never recorded an intent for."
            ),
            "category": "semantic_drift",
            "estimated_impact_usd": 71698.73,
            "confidence": "high",
            "verification_sql": _S4_VERIFICATION_SQL,
        }])
        verdict = _verdict("F1", "confirmed",
                           "1 row: u200 on 2024-03-02 has had_conversion=true, had_purchase_intent=false",
                           71698.73)
        memo = "Confirmed semantic drift: server conversions invisible to client attribution."

        mock = MockLLMClient([hypothesis, verdict, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        confirmed = [f for f in findings if f.get("status") == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["category"] == "semantic_drift"

    # ── Scenario 5: net financial impact confirmed ────────────────────────────

    def test_s5_net_impact_confirmed(self, make_scenario_db):
        """
        Client overcount and server undercount partially offset each other.
        The loop quantifies both sides: $300 overcount, $600 undercount, $300 net.
        """
        db = make_scenario_db(_S5_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "Overcount and undercount partially mask each other",
            "finding": (
                "Client overcounts by $300 (failed intents) while server undercounts "
                "by $600 (unattributed completions). Net server surplus: $300."
            ),
            "category": "undercount",
            "estimated_impact_usd": 300.00,
            "confidence": "high",
            "verification_sql": _S5_VERIFICATION_SQL,
        }])
        verdict = _verdict("F1", "confirmed",
                           "client_overcount=300.00, server_undercount=600.00 confirmed",
                           300.00)
        memo = "Confirmed: $300 net server surplus — $600 undercount offset by $300 overcount."

        mock = MockLLMClient([hypothesis, verdict, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        confirmed = [f for f in findings if f.get("status") == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["final_impact_usd"] == pytest.approx(300.00)

    # ── Scenario 6: ghost transaction for u201 ────────────────────────────────

    def test_s6_ghost_transaction_u201_confirmed(self, make_scenario_db):
        """
        u201 has 2 properly attributed completed txns AND 1 unattributed completion.
        Client LTV for u201 understates by $378.32.
        """
        db = make_scenario_db(_S6_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "User u201 ghost revenue — $378.32 attribution gap",
            "finding": (
                "User u201 has a completed server transaction (tx_1703, $378.32) "
                "with no client event. Client-side LTV for u201 is understated by $378.32."
            ),
            "category": "attribution_gap",
            "estimated_impact_usd": 378.32,
            "confidence": "high",
            "verification_sql": _S6_VERIFICATION_SQL,
        }])
        verdict = _verdict("F1", "confirmed",
                           "u201: server_confirmed=500.00, ghost_revenue=378.32 confirmed",
                           378.32)
        memo = "Confirmed: u201 has $378.32 in ghost revenue invisible to client attribution."

        mock = MockLLMClient([hypothesis, verdict, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        confirmed = [f for f in findings if f.get("status") == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["final_impact_usd"] == pytest.approx(378.32)
        assert "u201" in confirmed[0]["finding"]

    # ── Scenario 7: DECISIONS.md context is injected into system prompt ───────

    def test_s7_decisions_context_present_in_system_prompt(self):
        """
        When DECISIONS.md exists and tags match, _build_system_prompt() includes
        the decisions content. This is what prevents the loop from flagging
        intentional architecture (dedup, join direction) as data bugs.
        """
        from audit.agent import _build_system_prompt, _load_decisions
        from audit.loop import AUDIT_RELEVANT_TAGS

        decisions_path = Path("DECISIONS.md")
        if not decisions_path.exists():
            pytest.skip("DECISIONS.md not found — run from repo root")

        decisions = _load_decisions(path=decisions_path, tags=AUDIT_RELEVANT_TAGS)
        assert decisions, "Expected non-empty decisions content for [MODEL]/[AUDIT]/[INFRA] tags"

        sys_prompt = _build_system_prompt(decisions)

        # D-05 and D-06 are [MODEL] tagged and describe the join direction + taxonomy
        assert "server" in sys_prompt.lower(), "System prompt should reference server join decision"
        assert "DECISIONS" in sys_prompt or "D-0" in sys_prompt, (
            "System prompt should contain injected DECISIONS.md content"
        )

    def test_s7_empty_decisions_produces_base_prompt_only(self):
        """
        When no decisions are injected (empty string), the system prompt is the
        base role description with no DECISIONS.md block appended.
        """
        from audit.agent import _build_system_prompt

        sys_prompt = _build_system_prompt("")
        assert "ARCHITECTURAL CONTEXT" not in sys_prompt
        assert "senior data integrity auditor" in sys_prompt


# ═════════════════════════════════════════════════════════════════════════════
# Tier 2 — Loop structural guarantees
# ═════════════════════════════════════════════════════════════════════════════

class TestLoopStructuralGuarantees:
    """
    These tests verify loop robustness: malformed LLM output, SQL failures,
    and empty findings — the loop must not crash under any of these.
    """

    def test_loop_handles_malformed_hypothesis_json(self, make_scenario_db):
        """If Turn 1 returns unparseable text, loop returns the raw text as memo."""
        db = make_scenario_db()
        mock = MockLLMClient(["This is not JSON at all. Sorry."])
        memo, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())
        assert findings == []
        assert "not JSON" in memo or len(memo) > 0

    def test_loop_handles_failed_verification_sql(self, make_scenario_db):
        """If the LLM proposes a non-SELECT SQL, _safe_execute rejects it
        and the loop marks the finding as unverified (not crashed)."""
        db = make_scenario_db()

        hypothesis = _hypothesis([{
            "id": "F1",
            "title": "Attempted DDL injection",
            "finding": "Test finding",
            "category": "overcount",
            "estimated_impact_usd": 0.00,
            "confidence": "low",
            "verification_sql": "DROP TABLE fct_reconciliation",  # should be rejected
        }])
        verdict = _verdict("F1", "unverified", "SQL was rejected by safety gate", 0.00)
        memo = "No confirmed findings."

        mock = MockLLMClient([hypothesis, verdict, memo])
        _, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        # The finding should exist but not be confirmed
        assert len(findings) == 1
        assert findings[0].get("status") != "confirmed"

        # Verify the table still exists (DROP was blocked)
        conn = duckdb.connect(db, read_only=True)
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'fct_reconciliation'"
        ).fetchall()
        conn.close()
        assert len(tables) == 1, "fct_reconciliation must not have been dropped"

    def test_loop_returns_memo_even_with_zero_confirmed_findings(self, make_scenario_db):
        """If all findings are retracted, the loop still produces a memo."""
        db = make_scenario_db(_S3A_INSERT)

        hypothesis = _hypothesis([{
            "id": "F1", "title": "False hypothesis",
            "finding": "Imagined anomaly",
            "category": "overcount", "estimated_impact_usd": 0.0,
            "confidence": "low",
            "verification_sql": "SELECT COUNT(*) FROM main.fct_reconciliation WHERE 1=0",
        }])
        retraction = _verdict("F1", "retracted", "Zero rows returned. Finding retracted.", 0.0)
        memo = "After verification no findings were confirmed. The data appears clean."

        mock = MockLLMClient([hypothesis, retraction, memo])
        result_memo, findings = run_loop(db_path=db, llm_client=mock, decision_tags=set())

        assert result_memo  # memo must be non-empty
        assert all(f.get("status") == "retracted" for f in findings)
