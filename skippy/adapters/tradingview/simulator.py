"""
SKIPPY AI TRADER — PHASE 8

TradingView Local Signal Simulator

SIMULATION ONLY.
No networking or trade execution.
"""

from datetime import datetime, timezone

from skippy.adapters.tradingview.receiver import (
    TradingViewReceiver,
    TRADINGVIEW_SIGNAL_VALIDATED,
)
from skippy.kernel.event_bus import EventBus


def main():
    print("\n=== SKIPPY TRADINGVIEW SIMULATOR ===")

    bus = EventBus()
    receiver = TradingViewReceiver(bus)

    def observation_logger(signal):
        print("\n[SKIPPY EVENT RECEIVED]")
        print("Symbol:", signal["symbol"])
        print("Action:", signal["action"])
        print("Price:", signal["price"])
        print("Timestamp:", signal["timestamp"])

    bus.subscribe(
        TRADINGVIEW_SIGNAL_VALIDATED,
        observation_logger,
    )

    simulated_signal = {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    print("\nSending simulated TradingView signal...")

    receiver.receive(simulated_signal)

    print("\nEvent history count:", len(bus.history()))
    print("\nSIMULATION COMPLETE — NO TRADES EXECUTED")


if __name__ == "__main__":
    main()