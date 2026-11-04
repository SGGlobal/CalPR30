"""
SKIPPY AI TRADER - PHASE 8

Centralized Database Initialization Tests.
"""

import sqlite3

import pytest

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.database_initializer import (
    DatabaseInitializationError,
    TradingViewDatabaseInitializer,
)
from skippy.adapters.tradingview.observation_repository import (
    ObservationRepository,
)


FINGERPRINT = "a" * 64


def test_initializer_creates_required_tables(tmp_path):
    database = tmp_path / "initialized.db"

    initializer = TradingViewDatabaseInitializer(database)
    initializer.initialize()

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "signals",
        "processing_states",
        "observations",
    }.issubset(tables)


def test_repeated_initialization_preserves_signal(tmp_path):
    database = tmp_path / "initialized.db"

    initializer = TradingViewDatabaseInitializer(database)
    initializer.initialize()

    registry = AtomicSignalRegistry(database)
    assert registry.register(FINGERPRINT) is True

    initializer.initialize()

    assert registry.get_state(FINGERPRINT) == "RECEIVED"


def test_repeated_initialization_preserves_observation(tmp_path):
    database = tmp_path / "initialized.db"

    initializer = TradingViewDatabaseInitializer(database)
    initializer.initialize()

    repository = ObservationRepository(database)

    repository.save(
        FINGERPRINT,
        {"decision": "WATCH"},
    )

    original = repository.get(FINGERPRINT)

    initializer.initialize()

    assert repository.get(FINGERPRINT) == original


def test_schema_verification_succeeds(tmp_path):
    database = tmp_path / "initialized.db"

    initializer = TradingViewDatabaseInitializer(database)
    initializer.initialize()

    initializer.verify()


def test_missing_database_rejected(tmp_path):
    database = tmp_path / "missing.db"

    initializer = TradingViewDatabaseInitializer(database)

    with pytest.raises(DatabaseInitializationError):
        initializer.verify()


def test_missing_directory_rejected(tmp_path):
    database = tmp_path / "missing_directory" / "database.db"

    with pytest.raises(DatabaseInitializationError):
        TradingViewDatabaseInitializer(database)


def test_memory_database_rejected():
    with pytest.raises(ValueError):
        TradingViewDatabaseInitializer(":memory:")


def test_invalid_schema_detected(tmp_path):
    database = tmp_path / "invalid.db"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE signals (fingerprint TEXT)"
        )

    initializer = TradingViewDatabaseInitializer(database)

    with pytest.raises(DatabaseInitializationError):
        initializer.verify()


def test_initializer_supports_recovery_inspection(tmp_path):
    from skippy.adapters.tradingview.investigation_report import (
        InvestigationReport,
    )

    database = tmp_path / "initialized.db"

    TradingViewDatabaseInitializer(database).initialize()

    AtomicSignalRegistry(database).register(FINGERPRINT)

    report = InvestigationReport(database).generate()

    assert report["total_signals"] == 1
    assert report["summary"]["IN_PROGRESS"] == 1