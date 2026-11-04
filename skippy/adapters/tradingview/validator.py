"""
SKIPPY AI TRADER — Phase 8
TradingView Signal Validator

Local simulation only.
No network access or trade execution.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ALLOWED_SYMBOLS = frozenset({
    "BTCUSDT",
    "ETHUSDT",
})

ALLOWED_ACTIONS = frozenset({
    "BUY",
    "SELL",
    "WATCH",
})


@dataclass(frozen=True)
class ValidatedSignal:
    symbol: str
    action: str
    price: float
    timestamp: str


class SignalValidationError(ValueError):
    """Raised when a TradingView signal is invalid."""


def validate_signal(payload: dict[str, Any]) -> ValidatedSignal:
    """
    Validate a simulated TradingView signal.

    This function performs schema validation only.
    It does not authenticate the sender.
    """

    if not isinstance(payload, dict):
        raise SignalValidationError("Payload must be a dictionary")

    required = {"symbol", "action", "price", "timestamp"}

    missing = required - payload.keys()

    if missing:
        raise SignalValidationError(
            f"Missing required fields: {sorted(missing)}"
        )

    symbol = payload["symbol"]
    action = payload["action"]
    price = payload["price"]
    timestamp = payload["timestamp"]

    if not isinstance(symbol, str) or symbol not in ALLOWED_SYMBOLS:
        raise SignalValidationError("Unsupported trading symbol")

    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise SignalValidationError("Unsupported trading action")

    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise SignalValidationError("Price must be numeric")

    if not (0 < price < float("inf")):
        raise SignalValidationError("Price must be positive and finite")

    if not isinstance(timestamp, str):
        raise SignalValidationError("Timestamp must be a string")

    try:
        parsed = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SignalValidationError("Invalid timestamp") from exc

    if parsed.tzinfo is None:
        raise SignalValidationError(
            "Timestamp must include a timezone"
        )

    return ValidatedSignal(
        symbol=symbol,
        action=action,
        price=float(price),
        timestamp=parsed.astimezone(timezone.utc).isoformat(),
    )