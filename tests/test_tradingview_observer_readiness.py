"""
SKIPPY AI TRADER - PHASE 8

Durable Observer Readiness Enforcement Tests.

LOCAL SIMULATION ONLY.
No networking, automatic retries, or trade execution.
"""

import json
from datetime import datetime, timezone

import pytest

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.local_startup import (
    LocalStartupError,
    LocalTradingViewStartup,
)
from skippy.adapters.tradingview.receiver import TRADINGVIEW_SIGNAL_VALIDATED
from skippy.adapters.tradingview.validator import validate_signal
from skippy.adapters.tradingview.deduplicator import SignalDeduplicator


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def make_body():
    return json.dumps({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")


def fingerprint_for(body):
    validated = validate_signal(json.loads(body))
    return SignalDeduplicator().fingerprint(validated)


def test_valid_observer_allows_ingestion(tmp_path):
    database = tmp_path / "readiness.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    startup.ingest(make_body(), TEST_SECRET)

    assert startup.ready is True
    assert len(startup.observer.observations) == 1


def test_missing_observer_rejects_before_registration(tmp_path):
    database = tmp_path / "readiness.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    startup.event_bus.unsubscribe(
        TRADINGVIEW_SIGNAL_VALIDATED,
        startup.observer._observe,
    )

    body = make_body()
    fingerprint = fingerprint_for(body)

    with pytest.raises(LocalStartupError):
        startup.ingest(body, TEST_SECRET)

    assert AtomicSignalRegistry(database).get_state(fingerprint) is None
    assert startup.event_bus.history() == []


def test_duplicate_observer_rejects_before_registration(tmp_path):
    database = tmp_path / "readiness.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    startup.event_bus.subscribe(
        TRADINGVIEW_SIGNAL_VALIDATED,
        startup.observer._observe,
    )

    body = make_body()
    fingerprint = fingerprint_for(body)

    with pytest.raises(LocalStartupError):
        startup.ingest(body, TEST_SECRET)

    assert AtomicSignalRegistry(database).get_state(fingerprint) is None
    assert startup.event_bus.history() == []


def test_replaced_observer_rejects_before_registration(tmp_path):
    database = tmp_path / "readiness.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    startup.event_bus.unsubscribe(
        TRADINGVIEW_SIGNAL_VALIDATED,
        startup.observer._observe,
    )

    def replacement(payload):
        return None

    startup.event_bus.subscribe(
        TRADINGVIEW_SIGNAL_VALIDATED,
        replacement,
    )

    body = make_body()
    fingerprint = fingerprint_for(body)

    with pytest.raises(LocalStartupError):
        startup.ingest(body, TEST_SECRET)

    assert AtomicSignalRegistry(database).get_state(fingerprint) is None
    assert startup.event_bus.history() == []
