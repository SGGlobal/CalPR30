"""
SKIPPY AI TRADER - PHASE 8

Read-Only Recovery Inspector Tests.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.completion_coordinator import (
    ObservationCompletionCoordinator,
)
from skippy.adapters.tradingview.processing_state import ProcessingStateRegistry
from skippy.adapters.tradingview.recovery_inspector import (
    RecoveryInspectionError,
    RecoveryInspector,
)


FINGERPRINT = "a" * 64


def create_database(tmp_path):
    database = tmp_path / "recovery.db"
    AtomicSignalRegistry(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                fingerprint TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )

    return database


def test_recent_received_signal_is_in_progress(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    report = RecoveryInspector(database).inspect()

    assert report[0]["classification"] == "IN_PROGRESS"


def test_stale_received_signal_detected(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    old_time = (
        datetime.now(timezone.utc) - timedelta(minutes=20)
    ).isoformat()

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE processing_states
            SET updated_at = ?
            WHERE fingerprint = ?
            """,
            (old_time, FINGERPRINT),
        )
        connection.execute(
            """
            UPDATE signals
            SET first_seen = ?
            WHERE fingerprint = ?
            """,
            (old_time, FINGERPRINT),
        )

    report = RecoveryInspector(database).inspect()

    assert report[0]["classification"] == "STALE"


def test_processing_signal_detected(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    states = ProcessingStateRegistry(database)
    states.transition(FINGERPRINT, "PROCESSING")

    report = RecoveryInspector(database).inspect()

    assert report[0]["classification"] == "IN_PROGRESS"


def test_failed_signal_requires_review(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    states = ProcessingStateRegistry(database)
    states.transition(FINGERPRINT, "PROCESSING")
    states.transition(FINGERPRINT, "FAILED")

    report = RecoveryInspector(database).inspect()

    assert report[0]["classification"] == "REVIEW_REQUIRED"


def test_completed_signal_has_observation(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    states = ProcessingStateRegistry(database)
    states.transition(FINGERPRINT, "PROCESSING")

    coordinator = ObservationCompletionCoordinator(database)
    coordinator.complete(
        FINGERPRINT,
        {"decision": "WATCH"},
    )

    report = RecoveryInspector(database).inspect()

    assert report[0]["classification"] == "COMPLETED"


def test_completed_without_observation_is_inconsistent(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE processing_states
            SET state = 'COMPLETED'
            WHERE fingerprint = ?
            """,
            (FINGERPRINT,),
        )

    report = RecoveryInspector(database).inspect()

    assert report[0]["classification"] == "INCONSISTENT"


def test_missing_replay_record_is_inconsistent(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM signals WHERE fingerprint = ?",
            (FINGERPRINT,),
        )

    report = RecoveryInspector(database).inspect()

    assert report[0]["classification"] == "INCONSISTENT"


def test_inspection_does_not_modify_database(tmp_path):
    database = create_database(tmp_path)
    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA query_only = ON")

    inspector = RecoveryInspector(database)

    first = inspector.inspect()
    second = inspector.inspect()

    assert first == second

    with sqlite3.connect(database) as connection:
        state = connection.execute(
            """
            SELECT state FROM processing_states
            WHERE fingerprint = ?
            """,
            (FINGERPRINT,),
        ).fetchone()

    assert state == ("RECEIVED",)


def test_missing_database_rejected(tmp_path):
    with pytest.raises(RecoveryInspectionError):
        RecoveryInspector(tmp_path / "missing.db")


def test_invalid_stale_threshold_rejected(tmp_path):
    database = create_database(tmp_path)

    with pytest.raises(ValueError):
        RecoveryInspector(database, stale_after_seconds=0)


def test_invalid_inspection_time_rejected(tmp_path):
    database = create_database(tmp_path)

    with pytest.raises(ValueError):
        RecoveryInspector(database).inspect(
            datetime(2026, 11, 4)
        )


