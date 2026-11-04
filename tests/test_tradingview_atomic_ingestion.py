"""
SKIPPY AI TRADER - PHASE 8

Atomic Ingestion Integration Tests.

LOCAL SIMULATION ONLY.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.atomic_ingestion import (
    AtomicTradingViewIngestion,
)
from skippy.adapters.tradingview.deduplicator import DuplicateSignalError
from skippy.adapters.tradingview.freshness import StaleSignalError
from skippy.adapters.tradingview.observer import TradingViewObserver
from skippy.adapters.tradingview.receiver import TRADINGVIEW_SIGNAL_VALIDATED
from skippy.adapters.tradingview.security_gateway import WebhookSecurityError
from skippy.kernel.event_bus import EventBus


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def make_body(timestamp=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    return json.dumps({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": timestamp.isoformat(),
    }).encode("utf-8")


def make_ingestion(bus, database):
    return AtomicTradingViewIngestion(
        event_bus=bus,
        secret=TEST_SECRET,
        database_path=database,
    )


def test_valid_signal_reaches_hermes(tmp_path):
    bus = EventBus()
    observer = TradingViewObserver(bus)

    ingestion = make_ingestion(
        bus,
        tmp_path / "atomic.db",
    )

    result = ingestion.ingest(make_body(), TEST_SECRET)

    assert result["symbol"] == "BTCUSDT"
    assert len(bus.history()) == 1
    assert len(observer.observations) == 1


def test_atomic_registration_creates_received_state(tmp_path):
    database = tmp_path / "atomic.db"
    bus = EventBus()

    ingestion = make_ingestion(bus, database)

    body = make_body()

    ingestion.ingest(body, TEST_SECRET)

    payload = json.loads(body)
    from skippy.adapters.tradingview.validator import validate_signal

    validated = validate_signal(payload)

    fingerprint = ingestion.deduplicator.fingerprint(validated)

    assert ingestion.registry.get_state(fingerprint) == "RECEIVED"


def test_duplicate_rejected_after_restart(tmp_path):
    database = tmp_path / "atomic.db"
    body = make_body()

    first_bus = EventBus()
    first = make_ingestion(first_bus, database)

    first.ingest(body, TEST_SECRET)

    second_bus = EventBus()
    second = make_ingestion(second_bus, database)

    with pytest.raises(DuplicateSignalError):
        second.ingest(body, TEST_SECRET)

    assert len(first_bus.history()) == 1
    assert second_bus.history() == []


def test_invalid_authentication_prevents_registration(tmp_path):
    database = tmp_path / "atomic.db"
    bus = EventBus()

    ingestion = make_ingestion(bus, database)

    body = make_body()

    with pytest.raises(WebhookSecurityError):
        ingestion.ingest(body, "invalid-token")

    assert bus.history() == []

    ingestion.ingest(body, TEST_SECRET)

    assert len(bus.history()) == 1


def test_stale_signal_rejected(tmp_path):
    bus = EventBus()
    ingestion = make_ingestion(
        bus,
        tmp_path / "atomic.db",
    )

    old_timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)

    with pytest.raises(StaleSignalError):
        ingestion.ingest(
            make_body(old_timestamp),
            TEST_SECRET,
        )

    assert bus.history() == []


def test_subscriber_failure_preserves_received_state(tmp_path):
    bus = EventBus()

    def failing_subscriber(signal):
        raise RuntimeError("Simulated Hermes failure")

    bus.subscribe(
        TRADINGVIEW_SIGNAL_VALIDATED,
        failing_subscriber,
    )

    database = tmp_path / "atomic.db"

    ingestion = make_ingestion(bus, database)

    body = make_body()

    with pytest.raises(RuntimeError, match="Simulated Hermes failure"):
        ingestion.ingest(body, TEST_SECRET)

    from skippy.adapters.tradingview.validator import validate_signal

    validated = validate_signal(json.loads(body))
    fingerprint = ingestion.deduplicator.fingerprint(validated)

    assert ingestion.registry.get_state(fingerprint) == "RECEIVED"


def test_database_required():
    with pytest.raises(ValueError):
        AtomicTradingViewIngestion(
            event_bus=EventBus(),
            secret=TEST_SECRET,
            database_path=None,
        )