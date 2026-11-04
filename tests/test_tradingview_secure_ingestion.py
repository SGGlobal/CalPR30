"""
SKIPPY AI TRADER — PHASE 8

Secure Ingestion Integration Tests.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.deduplicator import DuplicateSignalError
from skippy.adapters.tradingview.freshness import StaleSignalError
from skippy.adapters.tradingview.observer import TradingViewObserver
from skippy.adapters.tradingview.secure_ingestion import SecureTradingViewIngestion
from skippy.adapters.tradingview.security_gateway import WebhookSecurityError
from skippy.adapters.tradingview.validator import SignalValidationError
from skippy.kernel.event_bus import EventBus


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def make_body(timestamp=None, price=65000):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    return json.dumps({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": price,
        "timestamp": timestamp.isoformat(),
    }).encode("utf-8")


def make_ingestion(tmp_path, bus=None):
    if bus is None:
        bus = EventBus()

    ingestion = SecureTradingViewIngestion(
        event_bus=bus,
        secret=TEST_SECRET,
        replay_database=tmp_path / "replay.db",
    )

    return ingestion, bus


def test_authenticated_signal_reaches_hermes(tmp_path):
    bus = EventBus()
    observer = TradingViewObserver(bus)
    ingestion, _ = make_ingestion(tmp_path, bus)

    result = ingestion.ingest(make_body(), TEST_SECRET)

    assert result["symbol"] == "BTCUSDT"
    assert len(bus.history()) == 1
    assert len(observer.observations) == 1


def test_invalid_token_rejected_before_publication(tmp_path):
    ingestion, bus = make_ingestion(tmp_path)

    with pytest.raises(WebhookSecurityError):
        ingestion.ingest(make_body(), "incorrect-token")

    assert bus.history() == []


def test_invalid_token_does_not_register_signal(tmp_path):
    ingestion, bus = make_ingestion(tmp_path)
    body = make_body()

    with pytest.raises(WebhookSecurityError):
        ingestion.ingest(body, "incorrect-token")

    ingestion.ingest(body, TEST_SECRET)

    assert len(bus.history()) == 1


def test_duplicate_rejected_after_restart(tmp_path):
    body = make_body()

    first, first_bus = make_ingestion(tmp_path)
    first.ingest(body, TEST_SECRET)

    second, second_bus = make_ingestion(tmp_path)

    with pytest.raises(DuplicateSignalError):
        second.ingest(body, TEST_SECRET)

    assert len(first_bus.history()) == 1
    assert second_bus.history() == []


def test_stale_signal_rejected(tmp_path):
    ingestion, bus = make_ingestion(tmp_path)

    old_time = datetime.now(timezone.utc) - timedelta(minutes=5)

    with pytest.raises(StaleSignalError):
        ingestion.ingest(make_body(old_time), TEST_SECRET)

    assert bus.history() == []


def test_invalid_price_rejected(tmp_path):
    ingestion, bus = make_ingestion(tmp_path)

    with pytest.raises(SignalValidationError):
        ingestion.ingest(make_body(price=-500), TEST_SECRET)

    assert bus.history() == []


def test_oversized_request_rejected(tmp_path):
    bus = EventBus()

    ingestion = SecureTradingViewIngestion(
        event_bus=bus,
        secret=TEST_SECRET,
        replay_database=tmp_path / "replay.db",
        max_payload_bytes=10,
    )

    with pytest.raises(WebhookSecurityError):
        ingestion.ingest(make_body(), TEST_SECRET)

    assert bus.history() == []


def test_persistent_database_required(tmp_path):
    with pytest.raises(ValueError):
        SecureTradingViewIngestion(
            event_bus=EventBus(),
            secret=TEST_SECRET,
            replay_database=None,
        )