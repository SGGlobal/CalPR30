"""
SKIPPY AI TRADER â€” PHASE 8

Duplicate Signal Protection Tests.
"""

import pytest

from skippy.adapters.tradingview.receiver import TradingViewReceiver
from skippy.adapters.tradingview.deduplicator import DuplicateSignalError
from datetime import datetime, timezone
from skippy.kernel.event_bus import EventBus


def valid_payload():
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def test_first_signal_accepted():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    receiver.receive(valid_payload())

    assert len(bus.history()) == 1


def test_duplicate_signal_rejected():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    payload = valid_payload()

    receiver.receive(payload)

    with pytest.raises(DuplicateSignalError):
        receiver.receive(payload)

    assert len(bus.history()) == 1


def test_different_signals_accepted():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    first = valid_payload()
    second = valid_payload()

    second["price"] = 66000

    receiver.receive(first)
    receiver.receive(second)

    assert len(bus.history()) == 2


def test_duplicate_does_not_publish_event():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    received = []

    bus.subscribe(
        "tradingview.signal.validated",
        received.append,
    )

    payload = valid_payload()

    receiver.receive(payload)

    with pytest.raises(DuplicateSignalError):
        receiver.receive(payload)

    assert len(received) == 1
