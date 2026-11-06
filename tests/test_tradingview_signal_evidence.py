"""
SKIPPY AI TRADER - PHASE 8

Durable Signal Evidence Tests.

LOCAL SIMULATION ONLY.
"""

import json
import os
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
from skippy.adapters.tradingview.observation_repository import (
    ObservationRepository,
)
from skippy.adapters.tradingview.recovery_inspector import RecoveryInspector
from skippy.adapters.tradingview.validator import validate_signal
from skippy.kernel.event_bus import EventBus


TEST_SECRET = "local-simulation-secret-32-characters-minimum"
LEGACY_FINGERPRINT = "a" * 64


def make_payload():
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def matching_fingerprint(payload: dict) -> str:
    validated = validate_signal(payload)
    return SignalDeduplicator().fingerprint(validated)


def test_evidence_persists_on_registration(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    assert registry.register(fingerprint, evidence=payload) is True

    stored = registry.get_evidence(fingerprint)

    assert stored["symbol"] == payload["symbol"]
    assert stored["action"] == payload["action"]
    assert stored["price"] == payload["price"]


def test_evidence_matches_fingerprint(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    registry.register(fingerprint, evidence=payload)

    stored = registry.get_evidence(fingerprint)
    recomputed = matching_fingerprint(stored)

    assert recomputed == fingerprint


def test_mismatched_fingerprint_rejected(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "evidence.db")

    payload = make_payload()

    with pytest.raises(ValueError, match="fingerprint"):
        registry.register(LEGACY_FINGERPRINT, evidence=payload)

    # No records created.
    assert registry.get_state(LEGACY_FINGERPRINT) is None


def test_extra_field_rejected_without_records(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "evidence.db")

    payload = make_payload()
    payload["unauthorized"] = "must-not-be-stored"

    with pytest.raises(ValueError, match="exactly"):
        registry.register(LEGACY_FINGERPRINT, evidence=payload)

    assert registry.get_state(LEGACY_FINGERPRINT) is None

    with sqlite3.connect(tmp_path / "evidence.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]

    assert count == 0


def test_duplicate_does_not_overwrite_evidence(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    original = make_payload()
    fingerprint = matching_fingerprint(original)

    registry.register(fingerprint, evidence=original)

    different = {
        "symbol": "ETHUSDT",
        "action": "SELL",
        "price": 3000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Different evidence has a different fingerprint; the same fingerprint
    # with different evidence must be rejected before mutation.
    with pytest.raises(ValueError):
        registry.register(fingerprint, evidence=different)

    assert registry.get_evidence(fingerprint)["symbol"] == original["symbol"]


def test_historical_null_evidence_remains_valid(tmp_path):
    database = tmp_path / "evidence.db"

    # Initialize the complete observational schema so RecoveryInspector
    # can join signals, processing_states, and observations.
    registry = AtomicSignalRegistry(database)
    ObservationRepository(database)

    # Simulate a pre-migration row with fingerprint only (NULL evidence).
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, NULL)
            """,
            (LEGACY_FINGERPRINT, datetime.now(timezone.utc).isoformat()),
        )
        connection.execute(
            """
            INSERT INTO processing_states (fingerprint, state, updated_at)
            VALUES (?, 'RECEIVED', ?)
            """,
            (LEGACY_FINGERPRINT, datetime.now(timezone.utc).isoformat()),
        )

    assert registry.get_evidence(LEGACY_FINGERPRINT) is None

    inspector = RecoveryInspector(database)
    assert inspector.get_evidence(LEGACY_FINGERPRINT) is None

    report = inspector.inspect()
    assert report[0]["fingerprint"] == LEGACY_FINGERPRINT
    assert "evidence" not in report[0]


def test_invalid_evidence_rejected(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "evidence.db")

    with pytest.raises(ValueError):
        registry.register(LEGACY_FINGERPRINT, evidence={"extra": "field"})

    with pytest.raises(ValueError):
        registry.register(LEGACY_FINGERPRINT, evidence="not-a-dict")

    with pytest.raises(ValueError):
        registry.register(
            LEGACY_FINGERPRINT,
            evidence={
                "symbol": "INVALID",
                "action": "BUY",
                "price": 1.0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # No records created.
    assert registry.get_state(LEGACY_FINGERPRINT) is None


def test_existing_database_migrates_with_historical_records(tmp_path):
    database = tmp_path / "legacy.db"

    old_time = "2026-01-01T00:00:00+00:00"

    # Create a pre-evidence schema with historical data.
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
        connection.execute(
            "INSERT INTO signals (fingerprint, first_seen) VALUES (?, ?)",
            (LEGACY_FINGERPRINT, old_time),
        )
        connection.execute(
            """
            INSERT INTO processing_states (fingerprint, state, updated_at)
            VALUES (?, 'RECEIVED', ?)
            """,
            (LEGACY_FINGERPRINT, old_time),
        )

    # Migration on open.
    registry = AtomicSignalRegistry(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(signals)")
        }
        row = connection.execute(
            "SELECT first_seen, evidence_json FROM signals WHERE fingerprint = ?",
            (LEGACY_FINGERPRINT,),
        ).fetchone()

    assert "evidence_json" in columns
    assert row[0] == old_time
    assert row[1] is None
    assert registry.get_state(LEGACY_FINGERPRINT) == "RECEIVED"


def test_repeated_initialization_preserves_records(tmp_path):
    database = tmp_path / "evidence.db"

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    first = AtomicSignalRegistry(database)
    first.register(fingerprint, evidence=payload)

    second = AtomicSignalRegistry(database)

    assert second.get_evidence(fingerprint)["symbol"] == payload["symbol"]
    assert second.register(fingerprint) is False


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


def test_interrupted_registration_leaves_evidence_verified(tmp_path):
    database = tmp_path / "evidence.db"
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    registry.register(fingerprint, evidence=payload)

    reopened = AtomicSignalRegistry(database)

    assert reopened.get_evidence(fingerprint)["symbol"] == payload["symbol"]
    assert reopened.get_state(fingerprint) == "RECEIVED"


def test_schema_migration_failure_fails_closed(tmp_path):
    with pytest.raises(AtomicRegistryError):
        AtomicSignalRegistry(tmp_path / "missing_dir" / "broken.db")


def test_sqlite_migration_failure_preserves_historical_records(tmp_path):
    """
    Cause a real SQLite ALTER TABLE failure on an existing database
    that already contains historical records.

    The database file is made read-only after the legacy schema and
    data are written, so the additive migration cannot proceed.
    """
    database = tmp_path / "readonly-legacy.db"

    old_time = "2026-01-01T00:00:00+00:00"

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
        connection.execute(
            "INSERT INTO signals (fingerprint, first_seen) VALUES (?, ?)",
            (LEGACY_FINGERPRINT, old_time),
        )
        connection.execute(
            """
            INSERT INTO processing_states (fingerprint, state, updated_at)
            VALUES (?, 'RECEIVED', ?)
            """,
            (LEGACY_FINGERPRINT, old_time),
        )

    # Make the file read-only so ALTER TABLE fails.
    os.chmod(database, 0o444)

    try:
        with pytest.raises(AtomicRegistryError):
            AtomicSignalRegistry(database)

        # Historical records must be unchanged.
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT first_seen FROM signals WHERE fingerprint = ?",
                (LEGACY_FINGERPRINT,),
            ).fetchone()

            columns = {
                col[1]
                for col in connection.execute("PRAGMA table_info(signals)")
            }

        assert row == (old_time,)
        assert "evidence_json" not in columns

    finally:
        # Restore writability so the temporary file can be cleaned up.
        os.chmod(database, 0o644)


def test_legacy_fingerprint_only_registration_still_works(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "evidence.db")

    assert registry.register(LEGACY_FINGERPRINT) is True
    assert registry.get_state(LEGACY_FINGERPRINT) == "RECEIVED"
    assert registry.get_evidence(LEGACY_FINGERPRINT) is None
