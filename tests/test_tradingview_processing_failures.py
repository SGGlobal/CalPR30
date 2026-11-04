"""
SKIPPY AI TRADER — PHASE 8

Observational Processing Failure Tests.

LOCAL SIMULATION ONLY.
No broker connections or trade execution.
"""

import json
from datetime import datetime, timezone

import pytest

from skippy.adapters.tradingview.deduplicator import DuplicateSignalError
from skippy.adapters.tradingview.receiver import TRADINGVIEW_SIGNAL_VALIDATED
from skippy.adapters.tradingview.secure_ingestion import SecureTradingViewIngestion
from skippy.kernel.event_bus import EventBus


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def make_body():
    return json.dumps({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")


def make_ingestion(bus, database):
    return SecureTradingViewIngestion(
        event_bus=bus,
        secret=TEST_SECRET,
        replay_database=database,
    )


def test_subscriber_failure_propagates(tmp_path):
    bus = EventBus()

    def failing_subscriber(signal):
        raise RuntimeError("Simulated observer failure")

    bus.subscribe(TRADINGVIEW_SIGNAL_VALIDATED, failing_subscriber)

    ingestion = make_ingestion(bus, tmp_path / "replay.db")

    with pytest.raises(RuntimeError, match="Simulated observer failure"):
        ingestion.ingest(make_body(), TEST_SECRET)

    assert len(bus.history()) == 1


def test_failed_signal_remains_registered(tmp_path):
    database = tmp_path / "replay.db"
    body = make_body()

    first_bus = EventBus()

    def failing_subscriber(signal):
        raise RuntimeError("Simulated processing failure")

    first_bus.subscribe(TRADINGVIEW_SIGNAL_VALIDATED, failing_subscriber)

    first_ingestion = make_ingestion(first_bus, database)

    with pytest.raises(RuntimeError):
        first_ingestion.ingest(body, TEST_SECRET)

    second_bus = EventBus()
    second_ingestion = make_ingestion(second_bus, database)

    with pytest.raises(DuplicateSignalError):
        second_ingestion.ingest(body, TEST_SECRET)

    assert second_bus.history() == []


def test_later_subscriber_not_called_after_failure(tmp_path):
    bus = EventBus()
    called = []

    def failing_subscriber(signal):
        raise RuntimeError("Simulated failure")

    def later_subscriber(signal):
        called.append(signal)

    bus.subscribe(TRADINGVIEW_SIGNAL_VALIDATED, failing_subscriber)
    bus.subscribe(TRADINGVIEW_SIGNAL_VALIDATED, later_subscriber)

    ingestion = make_ingestion(bus, tmp_path / "replay.db")

    with pytest.raises(RuntimeError):
        ingestion.ingest(make_body(), TEST_SECRET)

    assert called == []


def test_no_subscriber_still_records_event(tmp_path):
    bus = EventBus()
    ingestion = make_ingestion(bus, tmp_path / "replay.db")

    ingestion.ingest(make_body(), TEST_SECRET)

    assert len(bus.history()) == 1
