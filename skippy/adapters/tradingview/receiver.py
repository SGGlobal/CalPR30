"""
SKIPPY AI TRADER — PHASE 8

TradingView Local Signal Receiver

LOCAL SIMULATION ONLY.
No networking, broker connections, or trade execution.
"""

from dataclasses import asdict

from skippy.adapters.tradingview.validator import validate_signal
from skippy.adapters.tradingview.deduplicator import SignalDeduplicator
from skippy.kernel.event_bus import EventBus


TRADINGVIEW_SIGNAL_VALIDATED = "tradingview.signal.validated"


class TradingViewReceiver:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.deduplicator = SignalDeduplicator()

    def receive(self, payload: dict) -> dict:
        """
        Validate, deduplicate, and publish
        a simulated TradingView signal.
        """

        validated = validate_signal(payload)

        self.deduplicator.check(validated)

        signal_data = asdict(validated)

        self.event_bus.publish(
            TRADINGVIEW_SIGNAL_VALIDATED,
            signal_data,
        )

        return signal_data