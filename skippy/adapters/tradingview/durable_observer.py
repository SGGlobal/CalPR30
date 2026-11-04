"""
SKIPPY AI TRADER - PHASE 8

Durable TradingView Observer.

LOCAL SIMULATION ONLY.
No broker connections, automatic retries, or trade execution.
"""

from pathlib import Path

from skippy.adapters.tradingview.completion_coordinator import (
    ObservationCompletionCoordinator,
)
from skippy.adapters.tradingview.deduplicator import SignalDeduplicator
from skippy.adapters.tradingview.processing_state import ProcessingStateRegistry
from skippy.adapters.tradingview.receiver import TRADINGVIEW_SIGNAL_VALIDATED
from skippy.adapters.tradingview.validator import validate_signal
from skippy.reasoning.hermes import Hermes
from skippy.kernel.event_bus import EventBus


class DurableTradingViewObserver:
    def __init__(
        self,
        event_bus: EventBus,
        database_path: str | Path,
        hermes: Hermes | None = None,
    ):
        if database_path is None:
            raise ValueError("Durable observer requires persistent storage")

        self.event_bus = event_bus

        self.states = ProcessingStateRegistry(database_path)

        self.completion = ObservationCompletionCoordinator(
            database_path
        )

        self.deduplicator = SignalDeduplicator()

        self.hermes = hermes if hermes is not None else Hermes()

        self.observations = []

        self.event_bus.subscribe(
            TRADINGVIEW_SIGNAL_VALIDATED,
            self._observe,
        )

    def _observe(self, signal: dict) -> None:
        """
        Process a registered signal and persist its result.

        Subscriber exceptions propagate to the caller.
        No automatic retries are performed.
        """

        validated = validate_signal(signal)

        fingerprint = self.deduplicator.fingerprint(validated)

        self.states.transition(
            fingerprint,
            "PROCESSING",
        )

        try:
            observation = self.hermes.observe(signal)

            result = self.hermes.reason(
                observation,
                [],
            )

            self.completion.complete(
                fingerprint,
                result,
            )

        except Exception:
            # Preserve the failure in persistent storage.
            # A database failure here must also propagate.
            self.states.transition(
                fingerprint,
                "FAILED",
            )

            raise

        self.observations.append(result)