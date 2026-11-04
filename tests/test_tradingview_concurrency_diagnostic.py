"""
SKIPPY AI TRADER - PHASE 8

Concurrent Observer Diagnostic.

LOCAL SIMULATION ONLY.
No networking or trade execution.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.deduplicator import SignalDeduplicator
from skippy.adapters.tradingview.durable_observer import DurableTradingViewObserver
from skippy.adapters.tradingview.observation_repository import ObservationRepository
from skippy.adapters.tradingview.processing_state import ProcessingStateRegistry
from skippy.adapters.tradingview.receiver import TRADINGVIEW_SIGNAL_VALIDATED
from skippy.adapters.tradingview.validator import validate_signal
from skippy.kernel.event_bus import EventBus
from skippy.adapters.tradingview.processing_state import (
    ProcessingStateError,
)


SIGNAL = {
    "symbol": "BTCUSDT",
    "action": "BUY",
    "price": 65000,
    "timestamp": "2026-11-04T20:00:00+00:00",
}


class CountingHermes:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()


    def observe(self, signal):
        with self.lock:
            self.calls += 1

        time.sleep(0.2)
        return {"signal": signal}

    def reason(self, observation, memory):
        return {"decision": "WATCH"}


def test_concurrent_observers_do_not_duplicate_hermes(tmp_path):
    database = tmp_path / "concurrent.db"

    bus_a = EventBus()
    bus_b = EventBus()

    hermes = CountingHermes()

    DurableTradingViewObserver(
        event_bus=bus_a,
        database_path=database,
        hermes=hermes,
    )

    DurableTradingViewObserver(
        event_bus=bus_b,
        database_path=database,
        hermes=hermes,
    )

    fingerprint = SignalDeduplicator().fingerprint(
        validate_signal(SIGNAL)
    )

    registry = AtomicSignalRegistry(database)
    assert registry.register(fingerprint) is True

    start = threading.Barrier(3, timeout=3)

    def publish(bus):
        start.wait()

        try:
            bus.publish(
                TRADINGVIEW_SIGNAL_VALIDATED,
                SIGNAL,
            )
            return "COMPLETED"

        except ProcessingStateError:
            return "CLAIM_REJECTED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish, bus_a)
        second = executor.submit(publish, bus_b)

        start.wait()

        outcomes = [
            first.result(timeout=5),
            second.result(timeout=5),
        ]

    assert sorted(outcomes) == [
        "CLAIM_REJECTED",
        "COMPLETED",
    ]



    assert hermes.calls == 1

    assert ProcessingStateRegistry(database).get_state(
        fingerprint
    ) == "COMPLETED"

    assert ObservationRepository(database).get(
        fingerprint
    ) is not None
