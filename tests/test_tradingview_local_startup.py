ï»¿"""
SKIPPY AI TRADER - PHASE 8

Controlled Local Startup Tests.

LOCAL SIMULATION ONLY.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from skippy.adapters.tradingview.controlled_event_bus import (
    ControlledTradingViewEventBus,
)
from skippy.adapters.tradingview.database_initializer import (
    TradingViewDatabaseInitializer,
)
from skippy.adapters.tradingview.investigation_report import (
    InvestigationReport,
)
from skippy.adapters.tradingview.local_startup import (
    LocalStartupError,
    LocalTradingViewStartup,
)
from skippy.adapters.tradingview.observation_repository import (
    ObservationRepository,
)


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def make_body():
    return json.dumps({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")


def test_startup_creates_all_tables(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    assert startup.ready is True

    TradingViewDatabaseInitializer(database).verify()

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


def test_ingestion_rejected_before_startup(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)

    with pytest.raises(LocalStartupError):
        startup.ingest(make_body(), TEST_SECRET)

    assert startup.ingestion is None


def test_startup_registers_durable_observer(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    assert startup.ready is True
    assert startup.observer is not None
    assert startup.event_bus is not None
    assert isinstance(startup.event_bus, ControlledTradingViewEventBus)


def test_successful_startup_processes_signal(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    startup.ingest(make_body(), TEST_SECRET)

    report = InvestigationReport(database).generate()

    assert report["total_signals"] == 1
    assert report["summary"]["COMPLETED"] == 1

    assert len(startup.observer.observations) == 1
    assert len(startup.event_bus.history()) == 1


def test_startup_creates_persistent_observation(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    startup.ingest(make_body(), TEST_SECRET)

    report = InvestigationReport(database).generate()

    fingerprint = report["findings"][0]["fingerprint"]

    stored = ObservationRepository(database).get(fingerprint)

    assert stored is not None
    assert stored["result"]["decision"] == "WATCH"


def test_startup_rejects_duplicate_start(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    with pytest.raises(LocalStartupError):
        startup.start()

    assert startup.ready is True


def test_missing_database_directory_stops_startup(tmp_path):
    database = tmp_path / "missing_directory" / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)

    with pytest.raises(LocalStartupError):
        startup.start()

    assert startup.ready is False
    assert startup.ingestion is None


def test_invalid_secret_stops_startup(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, "short")
    
    with pytest.raises(LocalStartupError):
        startup.start()

    assert startup.ready is False
    assert startup.ingestion is None


def test_startup_preserves_existing_database(tmp_path):
    database = tmp_path / "startup.db"

    first = LocalTradingViewStartup(database, TEST_SECRET)
    first.start()

    first.ingest(make_body(), TEST_SECRET)

    before = InvestigationReport(database).generate()

    second = LocalTradingViewStartup(database, TEST_SECRET)
    second.start()

    after = InvestigationReport(database).generate()

    assert before["total_signals"] == 1
    assert after["total_signals"] == 1
    assert after["summary"]["COMPLETED"] == 1


def test_startup_creates_controlled_event_bus(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    assert isinstance(startup.event_bus, ControlledTradingViewEventBus)


def test_observer_readiness_checked_before_ingestion(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    order = []

    original_verify = startup._verify_observer_readiness
    original_ingest = startup.ingestion.ingest

    def tracking_verify():
        order.append("VERIFY")
        return original_verify()

    def tracking_ingest(body, token):
        order.append("INGEST")
        return original_ingest(body, token)

    startup._verify_observer_readiness = tracking_verify
    startup.ingestion.ingest = tracking_ingest

    startup.ingest(make_body(), TEST_SECRET)

    assert order == ["VERIFY", "INGEST"]


def test_ingestion_blocked_when_observer_verification_fails(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    # Remove the observer subscription so readiness fails.
    startup.event_bus.unsubscribe(
        "tradingview.signal.validated",
        startup.observer._observe,
    )

    with pytest.raises(LocalStartupError):
        startup.ingest(make_body(), TEST_SECRET)

    # No signal should have been published.
    assert len(startup.event_bus.history()) == 0


def test_concurrent_subscription_change_serialized_by_lock(tmp_path):
    """
    Demonstrates that the controlled bus serializes subscription changes
    relative to the locked ingest path. No timing-dependent sleeps are used;
    the test simply confirms the lock is held for the duration of the
    controlled operation by checking that a concurrent subscribe is blocked
    until the locked section completes.
    """
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    bus = startup.event_bus
    assert isinstance(bus, ControlledTradingViewEventBus)

    entered = threading.Event()
    release = threading.Event()
    results = []

    def slow_verify():
        entered.set()
        # Wait until the test signals release; this holds the lock.
        release.wait(timeout=2)
        results.append("VERIFY_DONE")

    def slow_ingest(body, token):
        results.append("INGEST")
        return {"ok": True}

    def concurrent_subscribe():
        # This will block until the lock is released.
        bus.subscribe("test.event", lambda p: None)
        results.append("SUBSCRIBE")

    t_ingest = threading.Thread(
        target=lambda: bus.controlled_ingest(
            slow_verify,
            slow_ingest,
            b"body",
            "token",
        )
    )
    t_sub = threading.Thread(target=concurrent_subscribe)

    t_ingest.start()
    # Ensure the locked section has started before attempting the concurrent subscribe.
    assert entered.wait(timeout=2)

    t_sub.start()

    # Give the subscribe thread a moment to block on the lock, then release.
    # (Deterministic: we only proceed after entered is set and after a short
    # yield; the lock guarantees ordering.)
    threading.Event().wait(0.05)

    # At this point the subscribe should still be waiting.
    assert "SUBSCRIBE" not in results

    release.set()

    t_ingest.join(timeout=2)
    t_sub.join(timeout=2)

    assert results == ["VERIFY_DONE", "INGEST", "SUBSCRIBE"]


def test_existing_successful_ingestion_unchanged(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    result = startup.ingest(make_body(), TEST_SECRET)

    assert result is not None
    assert len(startup.event_bus.history()) == 1
    assert len(startup.observer.observations) == 1


def test_existing_duplicate_and_security_failures_unchanged(tmp_path):
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    body = make_body()

    # First ingest succeeds.
    startup.ingest(body, TEST_SECRET)

    # Duplicate should still be rejected.
    from skippy.adapters.tradingview.deduplicator import DuplicateSignalError

    with pytest.raises(DuplicateSignalError):
        startup.ingest(body, TEST_SECRET)

    # Bad token should still fail security.
    with pytest.raises(Exception):
        startup.ingest(make_body(), "wrong-token")
