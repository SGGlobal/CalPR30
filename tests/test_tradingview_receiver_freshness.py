"""
SKIPPY AI TRADER — PHASE 8

Receiver Timestamp Freshness Integration Tests.
"""

from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.receiver import TradingViewReceiver
from skippy.adapters.tradingview.freshness import StaleSignalError
from skippy.kernel.event_bus import EventBus


def make_payload(timestamp):
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": timestamp.isoformat(),
    }


def test_current_signal_reaches_event_bus():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    payload = make_payload(datetime.now(timezone.utc))

    receiver.receive(payload)

    assert len(bus.history()) == 1


def test_stale_signal_never_reaches_event_bus():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)

    with pytest.raises(StaleSignalError):
        receiver.receive(make_payload(timestamp))

    assert bus.history() == []


def test_future_signal_never_reaches_event_bus():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    timestamp = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(StaleSignalError):
        receiver.receive(make_payload(timestamp))

    assert bus.history() == []


def test_stale_signal_does_not_reach_hermes():
    from skippy.adapters.tradingview.observer import TradingViewObserver

    bus = EventBus()
    observer = TradingViewObserver(bus)
    receiver = TradingViewReceiver(bus)

    timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)

    with pytest.raises(StaleSignalError):
        receiver.receive(make_payload(timestamp))

    assert observer.observations == []