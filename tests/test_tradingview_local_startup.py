"""
SKIPPY AI TRADER - PHASE 8

Controlled Local Startup Tests.

LOCAL SIMULATION ONLY.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

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
from skippy.kernel.event_bus import EventBus


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
    assert isinstance(startup.event_bus, EventBus)


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
