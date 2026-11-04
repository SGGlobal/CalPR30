"""
SKIPPY AI TRADER - PHASE 8

Local Startup Failure-Injection Tests.

LOCAL SIMULATION ONLY.
No external networking, automatic retries, or trade execution.
"""

import pytest

import skippy.adapters.tradingview.local_startup as startup_module
from skippy.adapters.tradingview.local_startup import (
    LocalStartupError,
    LocalTradingViewStartup,
)


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def test_database_verification_failure_stops_startup(tmp_path, monkeypatch):
    database = tmp_path / "startup.db"

    def fail_verification(self):
        raise RuntimeError("Simulated schema verification failure")

    monkeypatch.setattr(
        startup_module.TradingViewDatabaseInitializer,
        "verify",
        fail_verification,
    )

    startup = LocalTradingViewStartup(database, TEST_SECRET)

    with pytest.raises(LocalStartupError):
        startup.start()

    assert startup.ready is False
    assert startup.observer is None
    assert startup.ingestion is None
    assert startup.event_bus is None


def test_observer_registration_failure_stops_startup(tmp_path, monkeypatch):
    database = tmp_path / "startup.db"

    def fail_observer(*args, **kwargs):
        raise RuntimeError("Simulated observer registration failure")

    monkeypatch.setattr(
        startup_module,
        "DurableTradingViewObserver",
        fail_observer,
    )

    startup = LocalTradingViewStartup(database, TEST_SECRET)

    with pytest.raises(LocalStartupError):
        startup.start()

    assert startup.ready is False
    assert startup.observer is None
    assert startup.ingestion is None
    assert startup.event_bus is None


def test_ingestion_construction_failure_stops_startup(tmp_path, monkeypatch):
    database = tmp_path / "startup.db"

    def fail_ingestion(*args, **kwargs):
        raise RuntimeError("Simulated ingestion construction failure")

    monkeypatch.setattr(
        startup_module,
        "AtomicTradingViewIngestion",
        fail_ingestion,
    )

    startup = LocalTradingViewStartup(database, TEST_SECRET)

    with pytest.raises(LocalStartupError):
        startup.start()

    assert startup.ready is False
    assert startup.observer is None
    assert startup.ingestion is None
    assert startup.event_bus is None


def test_ingestion_rejected_after_failed_startup(tmp_path, monkeypatch):
    database = tmp_path / "startup.db"

    def fail_observer(*args, **kwargs):
        raise RuntimeError("Simulated observer failure")

    monkeypatch.setattr(
        startup_module,
        "DurableTradingViewObserver",
        fail_observer,
    )

    startup = LocalTradingViewStartup(database, TEST_SECRET)

    with pytest.raises(LocalStartupError):
        startup.start()

    with pytest.raises(LocalStartupError):
        startup.ingest(b"{}", TEST_SECRET)

    assert startup.ready is False


def test_duplicate_startup_preserves_existing_instance(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    original_bus = startup.event_bus
    original_observer = startup.observer
    original_ingestion = startup.ingestion

    with pytest.raises(LocalStartupError):
        startup.start()

    assert startup.ready is True
    assert startup.event_bus is original_bus
    assert startup.observer is original_observer
    assert startup.ingestion is original_ingestion
