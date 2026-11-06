"""
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
    relative to the locked ingest path using deterministic Event coordination.

    Limitation: This test proves that a concurrent subscribe is blocked
    until the locked controlled_ingest section completes. It does not
    exhaustively prove absence of all possible races under arbitrary
    scheduling; it verifies the lock is held across the protected interval.
    """
    database = tmp_path / "startup.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    bus = startup.event_bus
    assert isinstance(bus, ControlledTradingViewEventBus)

    lock_held = threading.Event()
    allow_release = threading.Event()
    subscribe_started = threading.Event()
    subscribe_finished = threading.Event()
    results = []
    results_lock = threading.Lock()

    def slow_verify():
        with results_lock:
            results.append("VERIFY_ENTER")
        lock_held.set()
        # Hold the bus lock until the test permits release.
        allow_release.wait(timeout=5)
        with results_lock:
            results.append("VERIFY_EXIT")

    def slow_ingest(body, token):
        with results_lock:
            results.append("INGEST")
        return {"ok": True}

    def concurrent_subscribe():
        subscribe_started.set()
        bus.subscribe("test.event", lambda p: None)
        with results_lock:
            results.append("SUBSCRIBE")
        subscribe_finished.set()

    t_ingest = threading.Thread(
        target=lambda: bus.controlled_ingest(
            slow_verify,
            slow_ingest,
            b"body",
            "token",
        ),
        daemon=True,
    )
    t_sub = threading.Thread(target=concurrent_subscribe, daemon=True)

    t_ingest.start()
    assert lock_held.wait(timeout=5), "locked section did not start"

    t_sub.start()
    assert subscribe_started.wait(timeout=5), "subscribe thread did not start"

    # At this point the subscribe thread has started and must be blocked
    # on the bus lock. It cannot have finished yet.
    assert not subscribe_finished.is_set()

    # Permit the locked section to complete.
    allow_release.set()

    t_ingest.join(timeout=5)
    t_sub.join(timeout=5)

    assert not t_ingest.is_alive()
    assert not t_sub.is_alive()
    assert subscribe_finished.is_set()

    with results_lock:
        assert results == ["VERIFY_ENTER", "VERIFY_EXIT", "INGEST", "SUBSCRIBE"]


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
