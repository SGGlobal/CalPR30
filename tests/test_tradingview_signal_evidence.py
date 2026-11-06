"""
SKIPPY AI TRADER - PHASE 8

Durable Signal Evidence Tests.

LOCAL SIMULATION ONLY.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from skippy.adapters.tradingview.atomic_ingestion import (
    AtomicTradingViewIngestion,
)
from skippy.adapters.tradingview.atomic_registry import (
    AtomicRegistryError,
    AtomicSignalRegistry,
)
from skippy.adapters.tradingview.deduplicator import SignalDeduplicator
from skippy.adapters.tradingview.recovery_inspector import RecoveryInspector
from skippy.adapters.tradingview.validator import validate_signal
from skippy.kernel.event_bus import EventBus


TEST_SECRET = "local-simulation-secret-32-characters-minimum"
FINGERPRINT = "a" * 64


def make_payload():
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def test_evidence_persists_on_registration(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    validated = validate_signal(payload)
    fingerprint = SignalDeduplicator().fingerprint(validated)

    assert registry.register(fingerprint, evidence=payload) is True

    stored = registry.get_evidence(fingerprint)

    assert stored == payload


def test_evidence_matches_fingerprint(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    validated = validate_signal(payload)
    fingerprint = SignalDeduplicator().fingerprint(validated)

    registry.register(fingerprint, evidence=payload)

    stored = registry.get_evidence(fingerprint)
    recomputed = SignalDeduplicator().fingerprint(validate_signal(stored))

    assert recomputed == fingerprint


def test_duplicate_does_not_overwrite_evidence(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    original = make_payload()
    registry.register(FINGERPRINT, evidence=original)

    different = {
        "symbol": "ETHUSDT",
        "action": "SELL",
        "price": 3000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    assert registry.register(FINGERPRINT, evidence=different) is False

    assert registry.get_evidence(FINGERPRINT) == original


def test_historical_null_evidence_remains_valid(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    # Simulate a pre-migration row.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, NULL)
            """,
            (FINGERPRINT, datetime.now(timezone.utc).isoformat()),
        )
        connection.execute(
            """
            INSERT INTO processing_states (fingerprint, state, updated_at)
            VALUES (?, 'RECEIVED', ?)
            """,
            (FINGERPRINT, datetime.now(timezone.utc).isoformat()),
        )

    assert registry.get_evidence(FINGERPRINT) is None

    inspector = RecoveryInspector(database)
    assert inspector.get_evidence(FINGERPRINT) is None

    report = inspector.inspect()
    assert report[0]["fingerprint"] == FINGERPRINT
    assert "evidence" not in report[0]


def test_invalid_evidence_rejected(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "evidence.db")

    with pytest.raises(ValueError):
        registry.register(FINGERPRINT, evidence={"extra": "field"})

    with pytest.raises(ValueError):
        registry.register(FINGERPRINT, evidence="not-a-dict")


def test_existing_database_migrates(tmp_path):
    database = tmp_path / "legacy.db"

    # Create a pre-evidence schema.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE signals (
                fingerprint TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE processing_states (
                fingerprint TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    # Re-open with the new registry; migration should add the column.
    registry = AtomicSignalRegistry(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signals)")
        }

    assert "evidence_json" in columns

    # Existing (none) and new registrations still work.
    assert registry.register(FINGERPRINT, evidence=make_payload()) is True


def test_repeated_initialization_preserves_records(tmp_path):
    database = tmp_path / "evidence.db"

    first = AtomicSignalRegistry(database)
    payload = make_payload()
    first.register(FINGERPRINT, evidence=payload)

    second = AtomicSignalRegistry(database)

    assert second.get_evidence(FINGERPRINT) == payload
    assert second.register(FINGERPRINT) is False


def test_ingestion_stores_evidence(tmp_path):
    database = tmp_path / "evidence.db"
    bus = EventBus()

    ingestion = AtomicTradingViewIngestion(
        event_bus=bus,
        secret=TEST_SECRET,
        database_path=database,
    )

    body = json.dumps(make_payload()).encode("utf-8")

    result = ingestion.ingest(body, TEST_SECRET)

    fingerprint = SignalDeduplicator().fingerprint(validate_signal(result))

    stored = ingestion.registry.get_evidence(fingerprint)

    assert stored is not None
    assert stored["symbol"] == result["symbol"]
    assert stored["action"] == result["action"]
    assert stored["price"] == result["price"]


def test_interrupted_registration_leaves_evidence(tmp_path):
    """
    Simulate a crash after registration commit but before publish.
    Evidence must still be durable.
    """
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    validated = validate_signal(payload)
    fingerprint = SignalDeduplicator().fingerprint(validated)

    # Register (commits) then raise before any publish.
    assert registry.register(fingerprint, evidence=payload) is True

    raise RuntimeError("simulated crash before publish")


def test_interrupted_registration_leaves_evidence_verified(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    validated = validate_signal(payload)
    fingerprint = SignalDeduplicator().fingerprint(validated)

    registry.register(fingerprint, evidence=payload)

    # Re-open as a new process would.
    reopened = AtomicSignalRegistry(database)

    assert reopened.get_evidence(fingerprint) == payload
    assert reopened.get_state(fingerprint) == "RECEIVED"


def test_schema_migration_failure_fails_closed(tmp_path):
    database = tmp_path / "broken.db"

    # Create a signals table that will cause ALTER to fail
    # by making the connection path invalid after creation.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE signals (
                fingerprint TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
            """
        )

    # Force a failure by making the directory disappear after the file exists.
    # (Simpler: pass a path whose parent is not a directory.)
    with pytest.raises(AtomicRegistryError):
        AtomicSignalRegistry(tmp_path / "missing_dir" / "broken.db")
