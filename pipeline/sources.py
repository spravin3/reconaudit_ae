"""
DLT source definitions for the reconaudit pipeline.

Design notes:
- Pagination is simulated via CHUNK_SIZE to mirror real API pagination behaviour.
- primary_key + write_disposition="merge" gives DLT-native deduplication on reload.
- All property access uses .get() so new fields on source records never raise.
- Column hints lock the schema so DLT won't silently widen types on schema evolution.
"""

import json
from pathlib import Path
from typing import Iterator

import dlt

CHUNK_SIZE = 500


def _load_json(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _flatten_client_event(raw: dict) -> dict:
    """
    Promote nested `properties` to top-level columns.
    Sparse keys (None for missing fields) drive graceful schema evolution:
    adding a new property to source data produces a nullable column, not a crash.
    """
    props = raw.get("properties") or {}
    return {
        "event_id":       raw["event_id"],
        "event_timestamp": raw["timestamp"],
        "user_id":        raw["user_id"],
        "event_name":     raw["event_name"],
        # page_view fields
        "url":            props.get("url"),
        "traffic_source": props.get("source"),
        # add_to_cart + purchase_intent fields
        "product_id":     props.get("product_id"),
        # purchase_intent only
        "intent_value":   props.get("value"),
    }


def _flatten_server_log(raw: dict) -> dict:
    meta = raw.get("meta") or {}
    return {
        "tx_id":        raw["tx_id"],
        "tx_timestamp": raw["timestamp"],
        "user_id":      raw["user_id"],
        "status":       raw["status"],
        "amount":       raw["amount"],
        "ext_id":       meta.get("ext_id"),
    }


@dlt.resource(
    name="client_events",
    primary_key="event_id",
    write_disposition="merge",
    columns={
        "event_id":        {"data_type": "text",      "nullable": False},
        "event_timestamp": {"data_type": "timestamp", "nullable": False},
        "user_id":         {"data_type": "text",      "nullable": False},
        "event_name":      {"data_type": "text",      "nullable": False},
        "url":             {"data_type": "text",      "nullable": True},
        "traffic_source":  {"data_type": "text",      "nullable": True},
        "product_id":      {"data_type": "text",      "nullable": True},
        "intent_value":    {"data_type": "double",    "nullable": True},
    },
)
def client_events_resource(data_path: str = "data") -> Iterator[dict]:
    """
    Paginated extraction from client_events.json.

    Yields CHUNK_SIZE records per page to simulate paginated API calls.
    DLT deduplicates on event_id using write_disposition=merge.
    """
    records = _load_json(Path(data_path) / "client_events.json")
    for page_start in range(0, len(records), CHUNK_SIZE):
        page = records[page_start : page_start + CHUNK_SIZE]
        for raw in page:
            yield _flatten_client_event(raw)


@dlt.resource(
    name="server_logs",
    primary_key="tx_id",
    write_disposition="merge",
    columns={
        "tx_id":        {"data_type": "text",      "nullable": False},
        "tx_timestamp": {"data_type": "timestamp", "nullable": False},
        "user_id":      {"data_type": "text",      "nullable": False},
        "status":       {"data_type": "text",      "nullable": False},
        "amount":       {"data_type": "double",    "nullable": False},
        "ext_id":       {"data_type": "text",      "nullable": True},
    },
)
def server_logs_resource(data_path: str = "data") -> Iterator[dict]:
    """
    Paginated extraction from server_logs.json.
    Deduplicates on tx_id; ext_id is nullable (225 records have no client link).
    """
    records = _load_json(Path(data_path) / "server_logs.json")
    for page_start in range(0, len(records), CHUNK_SIZE):
        page = records[page_start : page_start + CHUNK_SIZE]
        for raw in page:
            yield _flatten_server_log(raw)


@dlt.source(name="reconaudit")
def reconaudit_source(data_path: str = "data"):
    return [
        client_events_resource(data_path=data_path),
        server_logs_resource(data_path=data_path),
    ]
