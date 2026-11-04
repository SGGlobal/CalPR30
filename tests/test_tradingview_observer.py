"""
SKIPPY AI TRADER â€” PHASE 8

Hermes Observational Integration Tests.
"""

from skippy.adapters.tradingview.observer import TradingViewObserver
from skippy.adapters.tradingview.receiver import TradingViewReceiver
from datetime import datetime, timezone
from skippy.kernel.event_bus import EventBus


def valid_payload():
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def test_hermes_receives_signal():
    bus = EventBus()
    observer = TradingViewObserver(bus)
    receiver = TradingViewReceiver(bus)

    receiver.receive(valid_payload())

    assert len(observer.observations) == 1


def test_hermes_defaults_to_watch():
    bus = EventBus()
    observer = TradingViewObserver(bus)
    receiver = TradingViewReceiver(bus)

    receiver.receive(valid_payload())

    result = observer.observations[0]

    assert result["decision"] == "WATCH"


def test_hermes_preserves_observation():
    bus = EventBus()
    observer = TradingViewObserver(bus)
    receiver = TradingViewReceiver(bus)

    receiver.receive(valid_payload())

    result = observer.observations[0]

    assert result["observation"]["symbol"] == "BTCUSDT"
    assert result["observation"]["action"] == "BUY"


def test_invalid_signal_never_reaches_hermes():
    import pytest

    from skippy.adapters.tradingview.validator import SignalValidationError

    bus = EventBus()
    observer = TradingViewObserver(bus)
    receiver = TradingViewReceiver(bus)

    payload = valid_payload()
    payload["price"] = -100

    with pytest.raises(SignalValidationError):
        receiver.receive(payload)

    assert observer.observations == []
