"""
SKIPPY AI TRADER â€” PHASE 8

Local TradingView Receiver Tests.
"""

import pytest

from skippy.adapters.tradingview.receiver import (
    TradingViewReceiver,
    TRADINGVIEW_SIGNAL_VALIDATED,
)
from skippy.adapters.tradingview.validator import SignalValidationError
from datetime import datetime, timezone
from skippy.kernel.event_bus import EventBus


def valid_payload():
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def test_valid_signal_publishes_event():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    received_events = []

    bus.subscribe(
        TRADINGVIEW_SIGNAL_VALIDATED,
        received_events.append,
    )

    result = receiver.receive(valid_payload())

    assert result["symbol"] == "BTCUSDT"
    assert result["action"] == "BUY"

    assert len(received_events) == 1
    assert received_events[0] == result


def test_invalid_signal_does_not_publish():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    payload = valid_payload()
    payload["price"] = -100

    with pytest.raises(SignalValidationError):
        receiver.receive(payload)

    assert bus.history() == []


def test_event_history_records_signal():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    receiver.receive(valid_payload())

    history = bus.history()

    assert len(history) == 1
    assert history[0][0] == TRADINGVIEW_SIGNAL_VALIDATED


def test_multiple_valid_signals():
    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    first = valid_payload()
    second = valid_payload()

    second["symbol"] = "ETHUSDT"
    second["price"] = 3000

    receiver.receive(first)
    receiver.receive(second)

    assert len(bus.history()) == 2
