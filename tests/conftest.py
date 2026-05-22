"""
Pytest fixtures shared across pipeline tests.
"""

import json
import os
import tempfile
from pathlib import Path

import duckdb
import pytest

# ── Shared table schemas for scenario databases ───────────────────────────────
# These match the columns queried by _query_summary() and the verification
# SQLs the loop generates during the autonomous reasoning loop.

_BASE_SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS fct_revenue_summary (
        report_date              DATE,
        reconciliation_status    VARCHAR,
        tx_count                 INTEGER,
        server_revenue           DOUBLE,
        client_declared_revenue  DOUBLE,
        revenue_delta            DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fct_reconciliation (
        tx_id                    VARCHAR,
        tx_timestamp             TIMESTAMP,
        user_id                  VARCHAR,
        status                   VARCHAR,
        server_amount            DOUBLE,
        ext_id                   VARCHAR,
        event_id                 VARCHAR,
        client_timestamp         TIMESTAMP,
        intent_value             DOUBLE,
        product_id               VARCHAR,
        reconciliation_status    VARCHAR,
        amount_delta             DOUBLE,
        server_after_client      BOOLEAN,
        lag_minutes              INTEGER,
        report_date              DATE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fct_user_journey (
        user_id                  VARCHAR,
        activity_date            DATE,
        page_views               INTEGER,
        add_to_cart_count        INTEGER,
        unique_products_carted   INTEGER,
        purchase_intent_count    INTEGER,
        unique_products_intented INTEGER,
        total_intent_value       DOUBLE,
        primary_traffic_source   VARCHAR,
        primary_channel_type     VARCHAR,
        any_paid_session         BOOLEAN,
        total_txns               INTEGER,
        completed_txns           INTEGER,
        failed_txns              INTEGER,
        server_revenue           DOUBLE,
        had_page_view            BOOLEAN,
        had_add_to_cart          BOOLEAN,
        had_purchase_intent      BOOLEAN,
        had_conversion           BOOLEAN
    )
    """,
]

# Resolve data directory relative to repo root
REPO_ROOT = Path(__file__).parent.parent
DATA_PATH = str(REPO_ROOT / "data")


@pytest.fixture
def make_scenario_db(tmp_path):
    """
    Factory fixture for scenario-specific DuckDB files.

    Usage inside a test:
        def test_something(make_scenario_db):
            db = make_scenario_db(["INSERT INTO fct_reconciliation VALUES (...)"])
            # db is a str path to a DuckDB file with base schemas pre-created

    The factory:
      1. Creates all base tables (fct_reconciliation, fct_revenue_summary,
         fct_user_journey) so _query_summary() never fails on a missing table.
      2. Runs any extra SQL statements passed by the caller (inserts, etc.).
      3. Returns the file path as a str.

    Files are cleaned up automatically when the test completes (tmp_path scope).
    """
    _counter = [0]

    def _factory(extra_sqls: list[str] | None = None) -> str:
        db_path = str(tmp_path / f"scenario_{_counter[0]}.duckdb")
        _counter[0] += 1

        conn = duckdb.connect(db_path)
        for sql in _BASE_SCHEMA_SQL:
            conn.execute(sql)
        for sql in (extra_sqls or []):
            conn.execute(sql)
        conn.close()

        return db_path

    return _factory


@pytest.fixture(scope="session")
def data_path() -> str:
    return DATA_PATH


@pytest.fixture(scope="session")
def raw_client_events(data_path) -> list[dict]:
    with open(Path(data_path) / "client_events.json") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def raw_server_logs(data_path) -> list[dict]:
    with open(Path(data_path) / "server_logs.json") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def loaded_duckdb():
    """
    Runs the full DLT pipeline into a temp DuckDB file and returns (connection, schema_name).
    dev_mode=False so DLT uses the dataset_name as-is (no timestamp suffix).
    Scoped to session so the pipeline runs once and all tests share the result.
    """
    import dlt
    from pipeline.sources import reconaudit_source

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = str(Path(tmpdir) / "test.duckdb")
        pipeline = dlt.pipeline(
            pipeline_name="test_reconaudit",
            destination=dlt.destinations.duckdb(credentials=db_file),
            dataset_name="raw",
            dev_mode=False,
        )
        pipeline.run(reconaudit_source(data_path=DATA_PATH))

        conn = duckdb.connect(db_file, read_only=True)
        # Discover actual schema name DLT created (may vary by version)
        schemas = [
            r[0] for r in conn.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'main')"
            ).fetchall()
        ]
        raw_schema = next(s for s in schemas if "raw" in s and "staging" not in s)
        yield conn, raw_schema
        conn.close()
