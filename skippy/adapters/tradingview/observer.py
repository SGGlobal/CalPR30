"""
SKIPPY AI TRADER — PHASE 8

Hermes TradingView Observer

LOCAL OBSERVATION ONLY.
No broker connections or trade execution.
"""

from skippy.reasoning.hermes import Hermes
from skippy.adapters.tradingview.receiver import (
    TRADINGVIEW_SIGNAL_VALIDATED,
)


class TradingViewObserver:
    def __init__(self, event_bus):
        self.hermes = Hermes()
        self.observations = []

        event_bus.subscribe(
            TRADINGVIEW_SIGNAL_VALIDATED,
            self.observe,
        )

    def observe(self, signal):
        observation = self.hermes.observe(signal)

        # Empty memory context for initial testing.
        # This does not evaluate market performance.
        result = self.hermes.reason(observation, [])

        self.observations.append(result)

        print("\n[HERMES OBSERVATION]")
        print("Symbol:", signal["symbol"])
        print("Incoming action:", signal["action"])
        print("Hermes decision:", result["decision"])
        print("Memory count:", result["confidence"])
        print("Execution: DISABLED")

        return result