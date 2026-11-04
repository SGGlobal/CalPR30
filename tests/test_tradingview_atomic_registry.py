"""
SKIPPY AI TRADER — PHASE 8

Atomic Registration Tests.
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from skippy.adapters.tradingview.atomic_registry import (
    AtomicRegistryError,
    AtomicSignalRegistry,
)


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64


def test_new_signal_creates_both_records(tmp_path):
    database = tmp_path / "atomic.db"
    registry = AtomicSignalRegistry(database)

    assert registry.register(FINGERPRINT_A) is True
    assert registry.get_state(FINGERPRINT_A) == "RECEIVED"

    with sqlite3.connect(database) as connection:
        replay = connection.execute(
            "SELECT fingerprint FROM signals WHERE fingerprint = ?",
            (FINGERPRINT_A,),
        ).fetchone()

        state = connection.execute(
            "SELECT state FROM processing_states WHERE fingerprint = ?",
            (FINGERPRINT_A,),
        ).fetchone()

    assert replay is not None
    assert state == ("RECEIVED",)


def test_duplicate_rejected(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "atomic.db")

    assert registry.register(FINGERPRINT_A) is True
    assert registry.register(FINGERPRINT_A) is False


def test_registration_survives_restart(tmp_path):
    database = tmp_path / "atomic.db"

    first = AtomicSignalRegistry(database)
    assert first.register(FINGERPRINT_A) is True

    second = AtomicSignalRegistry(database)

    assert second.register(FINGERPRINT_A) is False
    assert second.get_state(FINGERPRINT_A) == "RECEIVED"


def test_different_signals_accepted(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "atomic.db")

    assert registry.register(FINGERPRINT_A) is True
    assert registry.register(FINGERPRINT_B) is True


def test_invalid_fingerprint_rejected(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "atomic.db")

    with pytest.raises(ValueError):
        registry.register("invalid")


def test_missing_directory_rejected(tmp_path):
    with pytest.raises(AtomicRegistryError):
        AtomicSignalRegistry(tmp_path / "missing" / "atomic.db")


def test_concurrent_duplicate_registration(tmp_path):
    registry = AtomicSignalRegistry(tmp_path / "atomic.db")

    def register():
        return registry.register(FINGERPRINT_A)

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: register(), range(5)))

    assert results.count(True) == 1
    assert results.count(False) == 4


def test_failed_state_insert_rolls_back_replay_record(tmp_path):
    database = tmp_path / "atomic.db"
    registry = AtomicSignalRegistry(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_state_insert
            BEFORE INSERT ON processing_states
            BEGIN
                SELECT RAISE(ABORT, 'simulated state failure');
            END;
            """
        )

    with pytest.raises(AtomicRegistryError):
        registry.register(FINGERPRINT_A)

    with sqlite3.connect(database) as connection:
        replay = connection.execute(
            "SELECT fingerprint FROM signals WHERE fingerprint = ?",
            (FINGERPRINT_A,),
        ).fetchone()

        state = connection.execute(
            "SELECT state FROM processing_states WHERE fingerprint = ?",
            (FINGERPRINT_A,),
        ).fetchone()

    assert replay is None
    assert state is None


def test_existing_replay_record_not_overwritten(tmp_path):
    database = tmp_path / "atomic.db"
    registry = AtomicSignalRegistry(database)

    assert registry.register(FINGERPRINT_A) is True

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE processing_states
            SET state = 'PROCESSING'
            WHERE fingerprint = ?
            """,
            (FINGERPRINT_A,),
        )

    assert registry.register(FINGERPRINT_A) is False
    assert registry.get_state(FINGERPRINT_A) == "PROCESSING"