"""
Pipeline tests covering three axes:
  1. Pagination simulation — chunked extraction produces the same result as a full load.
  2. Deduplication — DLT primary_key merge removes the 605 duplicate page_view events.
  3. Schema evolution — extractor is resilient to new or missing properties fields.
"""

import json
import tempfile
from pathlib import Path

import pytest

from pipeline.sources import (
    _flatten_client_event,
    _flatten_server_log,
    client_events_resource,
    server_logs_resource,
)


# ---------------------------------------------------------------------------
# Pagination simulation
# ---------------------------------------------------------------------------

class TestPaginationSimulation:
    """
    The extractor yields records in CHUNK_SIZE batches.
    Total records yielded must equal total records in the source file,
    and no record may be dropped at a page boundary.
    """

    def test_client_events_count_matches_source(self, raw_client_events):
        extracted = list(client_events_resource())
        assert len(extracted) == len(raw_client_events), (
            f"Expected {len(raw_client_events)} records, got {len(extracted)}"
        )

    def test_client_events_no_records_dropped_at_page_boundary(self, raw_client_events):
        source_ids = {e["event_id"] for e in raw_client_events}
        extracted_ids = {r["event_id"] for r in client_events_resource()}
        missing = source_ids - extracted_ids
        assert not missing, f"event_ids dropped during pagination: {missing}"

    def test_server_logs_count_matches_source(self, raw_server_logs):
        extracted = list(server_logs_resource())
        assert len(extracted) == len(raw_server_logs)

    def test_server_logs_no_records_dropped_at_page_boundary(self, raw_server_logs):
        source_ids = {e["tx_id"] for e in raw_server_logs}
        extracted_ids = {r["tx_id"] for r in server_logs_resource()}
        missing = source_ids - extracted_ids
        assert not missing, f"tx_ids dropped during pagination: {missing}"

    def test_all_pages_covered(self, raw_client_events):
        """Spot-check: the last record in the source appears in the extraction."""
        last_source_id = raw_client_events[-1]["event_id"]
        extracted_ids = {r["event_id"] for r in client_events_resource()}
        assert last_source_id in extracted_ids


# ---------------------------------------------------------------------------
# Deduplication — DLT layer (in-process via loaded_duckdb fixture)
# ---------------------------------------------------------------------------

class TestDeduplication:
    """
    DLT write_disposition=merge with primary_key deduplicates on reload.
    After a full pipeline run, the raw schema's client_events must have unique event_ids.
    """

    def test_client_events_unique_after_load(self, loaded_duckdb):
        conn, schema = loaded_duckdb
        dup_count = conn.execute(f"""
            select count(*) from (
                select event_id, count(*) as cnt
                from {schema}.client_events
                group by event_id
                having cnt > 1
            )
        """).fetchone()[0]
        assert dup_count == 0, (
            f"{dup_count} event_ids have duplicates after DLT load"
        )

    def test_server_logs_unique_after_load(self, loaded_duckdb):
        conn, schema = loaded_duckdb
        dup_count = conn.execute(f"""
            select count(*) from (
                select tx_id, count(*) as cnt
                from {schema}.server_logs
                group by tx_id
                having cnt > 1
            )
        """).fetchone()[0]
        assert dup_count == 0

    def test_client_events_deduped_count(self, loaded_duckdb, raw_client_events):
        """After dedup, row count should equal unique event_id count in source."""
        conn, schema = loaded_duckdb
        expected_unique = len({e["event_id"] for e in raw_client_events})
        actual = conn.execute(
            f"select count(*) from {schema}.client_events"
        ).fetchone()[0]
        assert actual == expected_unique, (
            f"Expected {expected_unique} unique events, got {actual}"
        )

    def test_purchase_intent_count_unchanged_by_dedup(self, loaded_duckdb, raw_client_events):
        """
        No purchase_intent events are duplicated in source, so dedup must not
        reduce their count.
        """
        conn, schema = loaded_duckdb
        expected = sum(1 for e in raw_client_events if e["event_name"] == "purchase_intent")
        actual = conn.execute(f"""
            select count(*) from {schema}.client_events
            where event_name = 'purchase_intent'
        """).fetchone()[0]
        assert actual == expected


# ---------------------------------------------------------------------------
# Schema evolution
# ---------------------------------------------------------------------------

class TestSchemaEvolution:
    """
    The flattening functions must handle:
      a) New fields added to properties (forward evolution)
      b) Expected fields missing from properties (backward regression)
      c) Entirely missing 'properties' key
    """

    def test_new_property_field_ignored_gracefully(self):
        """A new 'currency' field on purchase_intent must not raise."""
        raw = {
            "event_id": "a9999",
            "timestamp": "2024-03-02T10:00:00Z",
            "user_id": "u100",
            "event_name": "purchase_intent",
            "properties": {
                "product_id": "p_999",
                "value": 99.99,
                "currency": "USD",       # new field — not in original schema
                "discount_code": "SAVE10",  # another new field
            },
        }
        result = _flatten_client_event(raw)
        assert result["intent_value"] == 99.99
        assert result["product_id"] == "p_999"
        # new fields are not surfaced (extractor uses explicit mapping, not **kwargs)
        assert "currency" not in result
        assert "discount_code" not in result

    def test_missing_value_field_returns_none(self):
        """A purchase_intent missing 'value' must yield intent_value=None, not raise."""
        raw = {
            "event_id": "a8888",
            "timestamp": "2024-03-02T10:00:00Z",
            "user_id": "u100",
            "event_name": "purchase_intent",
            "properties": {"product_id": "p_888"},  # value intentionally absent
        }
        result = _flatten_client_event(raw)
        assert result["intent_value"] is None

    def test_missing_properties_key_returns_all_none(self):
        """An event with no 'properties' key at all must not raise."""
        raw = {
            "event_id": "a7777",
            "timestamp": "2024-03-02T10:00:00Z",
            "user_id": "u100",
            "event_name": "page_view",
        }
        result = _flatten_client_event(raw)
        assert result["url"] is None
        assert result["traffic_source"] is None
        assert result["product_id"] is None
        assert result["intent_value"] is None

    def test_null_meta_ext_id_returns_none(self):
        """Server log records with meta.ext_id: null must yield ext_id=None."""
        raw = {
            "tx_id": "tx_1909",
            "timestamp": "2024-03-02T01:35:00Z",
            "user_id": "u201",
            "status": "completed",
            "amount": 378.32,
            "meta": {"ext_id": None},
        }
        result = _flatten_server_log(raw)
        assert result["ext_id"] is None

    def test_missing_meta_key_returns_none(self):
        """Server log records with no 'meta' key at all must not raise."""
        raw = {
            "tx_id": "tx_0001",
            "timestamp": "2024-03-02T01:35:00Z",
            "user_id": "u100",
            "status": "completed",
            "amount": 100.00,
        }
        result = _flatten_server_log(raw)
        assert result["ext_id"] is None

    def test_evolved_source_written_to_temp_db(self, data_path, raw_client_events):
        """
        End-to-end: an evolved source file (new field added) loads without error
        and produces the correct record count.
        """
        import dlt

        evolved = []
        for e in raw_client_events:
            ev = {**e, "properties": dict(e.get("properties") or {})}
            if ev["event_name"] == "purchase_intent":
                ev["properties"]["currency"] = "USD"
            evolved.append(ev)

        with tempfile.TemporaryDirectory() as tmpdir:
            evolved_data = Path(tmpdir) / "data"
            evolved_data.mkdir()
            # Write evolved client_events; server_logs unchanged
            (evolved_data / "client_events.json").write_text(
                json.dumps(evolved), encoding="utf-8"
            )
            import shutil
            shutil.copy(
                Path(data_path) / "server_logs.json",
                evolved_data / "server_logs.json",
            )

            from pipeline.sources import reconaudit_source
            db_file = str(Path(tmpdir) / "evolved.duckdb")
            pipeline = dlt.pipeline(
                pipeline_name="test_evolved",
                destination=dlt.destinations.duckdb(credentials=db_file),
                dataset_name="raw",
                dev_mode=True,
            )
            info = pipeline.run(reconaudit_source(data_path=str(evolved_data)))

            import duckdb as _duckdb
            conn = _duckdb.connect(db_file, read_only=True)
            schemas = [
                r[0] for r in conn.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('information_schema','pg_catalog','main')"
                ).fetchall()
            ]
            raw_schema = next(s for s in schemas if "raw" in s and "staging" not in s)
            count = conn.execute(
                f"select count(*) from {raw_schema}.client_events"
            ).fetchone()[0]
            conn.close()

        expected_unique = len({e["event_id"] for e in raw_client_events})
        assert count == expected_unique
