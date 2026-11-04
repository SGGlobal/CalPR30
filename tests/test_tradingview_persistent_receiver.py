"""
SKIPPY AI TRADER — PHASE 8

Persistent Replay Receiver Integration Tests.
"""

from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.deduplicator import DuplicateSignalError
from skippy.adapters.tradingview.freshness import StaleSignalError
from skippy.adapters.tradingview.receiver import TradingViewReceiver
from skippy.kernel.event_bus import EventBus


def make_payload(timestamp=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": timestamp.isoformat(),
    }


def test_persistent_signal_accepted(tmp_path):
    bus = EventBus()
    receiver = TradingViewReceiver(
        bus,
        replay_database=tmp_path / "replay.db",
    )

    receiver.receive(make_payload())

    assert len(bus.history()) == 1


def test_duplicate_rejected_after_restart(tmp_path):
    database = tmp_path / "replay.db"
    payload = make_payload()

    first_bus = EventBus()
    first_receiver = TradingViewReceiver(
        first_bus,
        replay_database=database,
    )

    first_receiver.receive(payload)

    second_bus = EventBus()
    second_receiver = TradingViewReceiver(
        second_bus,
        replay_database=database,
    )

    with pytest.raises(DuplicateSignalError):
        second_receiver.receive(payload)

    assert second_bus.history() == []


def test_different_signals_survive_restart(tmp_path):
    database = tmp_path / "replay.db"

    first_receiver = TradingViewReceiver(
        EventBus(),
        replay_database=database,
    )

    first_receiver.receive(make_payload())

    second_bus = EventBus()
    second_receiver = TradingViewReceiver(
        second_bus,
        replay_database=database,
    )

    different = make_payload()
    different["price"] = 66000

    second_receiver.receive(different)

    assert len(second_bus.history()) == 1


def test_stale_signal_not_registered(tmp_path):
    database = tmp_path / "replay.db"

    bus = EventBus()
    receiver = TradingViewReceiver(
        bus,
        replay_database=database,
    )

    old_timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)

    with pytest.raises(StaleSignalError):
        receiver.receive(make_payload(old_timestamp))

    assert bus.history() == []


def test_duplicate_never_reaches_hermes(tmp_path):
    from skippy.adapters.tradingview.observer import TradingViewObserver

    database = tmp_path / "replay.db"
    payload = make_payload()

    first_bus = EventBus()
    first_receiver = TradingViewReceiver(
        first_bus,
        replay_database=database,
    )

    first_receiver.receive(payload)

    second_bus = EventBus()
    observer = TradingViewObserver(second_bus)

    second_receiver = TradingViewReceiver(
        second_bus,
        replay_database=database,
    )

    with pytest.raises(DuplicateSignalError):
        second_receiver.receive(payload)

    assert observer.observations == []