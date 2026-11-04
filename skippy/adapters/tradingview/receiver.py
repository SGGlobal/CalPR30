"""
SKIPPY AI TRADER — PHASE 8

TradingView Local Signal Receiver

LOCAL SIMULATION ONLY.
No networking, broker connections, or trade execution.
"""

from dataclasses import asdict
from pathlib import Path

from skippy.adapters.tradingview.deduplicator import (
    DuplicateSignalError,
    SignalDeduplicator,
)
from skippy.adapters.tradingview.freshness import SignalFreshnessValidator
from skippy.adapters.tradingview.replay_registry import PersistentReplayRegistry
from skippy.adapters.tradingview.validator import validate_signal
from skippy.kernel.event_bus import EventBus


TRADINGVIEW_SIGNAL_VALIDATED = "tradingview.signal.validated"


class TradingViewReceiver:
    def __init__(
        self,
        event_bus: EventBus,
        replay_database: str | Path | None = None,
    ):
        self.event_bus = event_bus
        self.deduplicator = SignalDeduplicator()
        self.freshness = SignalFreshnessValidator()

        self.replay_registry = (
            PersistentReplayRegistry(replay_database)
            if replay_database is not None
            else None
        )

    def receive(self, payload: dict) -> dict:
        """
        Validate, check freshness, reject duplicates,
        and publish a simulated TradingView signal.
        """

        validated = validate_signal(payload)

        self.freshness.check(validated)

        fingerprint = self.deduplicator.fingerprint(validated)

        if self.replay_registry is not None:
            registered = self.replay_registry.register(
                fingerprint,
                validated.timestamp,
            )

            if not registered:
                raise DuplicateSignalError(
                    "Persistent duplicate TradingView signal rejected"
                )
        else:
            self.deduplicator.check(validated)

        signal_data = asdict(validated)

        self.event_bus.publish(
            TRADINGVIEW_SIGNAL_VALIDATED,
            signal_data,
        )

        return signal_data