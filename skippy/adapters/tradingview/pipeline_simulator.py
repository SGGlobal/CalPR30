"""
SKIPPY AI TRADER — PHASE 8

End-to-End TradingView-to-Hermes Simulator

LOCAL SIMULATION ONLY.
No broker connections or trade execution.
"""

from datetime import datetime, timezone

from skippy.adapters.tradingview.receiver import TradingViewReceiver
from skippy.adapters.tradingview.observer import TradingViewObserver
from skippy.adapters.tradingview.deduplicator import DuplicateSignalError
from skippy.adapters.tradingview.validator import SignalValidationError
from skippy.kernel.event_bus import EventBus


def main():
    print("\n======================================")
    print(" SKIPPY AI TRADER — PHASE 8")
    print(" TRADINGVIEW → HERMES SIMULATION")
    print("======================================")

    bus = EventBus()
    observer = TradingViewObserver(bus)
    receiver = TradingViewReceiver(bus)

    timestamp = datetime.now(timezone.utc).isoformat()

    signal = {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": timestamp,
    }

    print("\n[1] Sending simulated signal")

    receiver.receive(signal)

    print("\n[2] Testing duplicate protection")

    try:
        receiver.receive(signal)
    except DuplicateSignalError:
        print("Duplicate signal correctly rejected")

    print("\n[3] Testing invalid signal protection")

    invalid_signal = signal.copy()
    invalid_signal["price"] = -500

    try:
        receiver.receive(invalid_signal)
    except SignalValidationError:
        print("Invalid signal correctly rejected")

    print("\n[4] Reviewing Hermes observations")

    for index, result in enumerate(observer.observations, start=1):
        print(f"\nObservation #{index}")
        print("Decision:", result["decision"])
        print("Memory count:", result["confidence"])

    print("\n[5] Event bus history")

    print("Events recorded:", len(bus.history()))

    print("\n======================================")
    print(" SIMULATION COMPLETE")
    print(" NO TRADES EXECUTED")
    print("======================================")


if __name__ == "__main__":
    main()