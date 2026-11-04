"""
SKIPPY AI TRADER — PHASE 8

Timestamp Freshness Validator

LOCAL SIMULATION ONLY.
No network access or trade execution.
"""

from datetime import datetime, timezone


class StaleSignalError(ValueError):
    """Raised when a signal is outside the permitted time window."""


class SignalFreshnessValidator:
    def __init__(
        self,
        max_age_seconds: int = 120,
        max_future_seconds: int = 10,
    ):
        if max_age_seconds < 0 or max_future_seconds < 0:
            raise ValueError("Freshness limits must be nonnegative")

        self.max_age_seconds = max_age_seconds
        self.max_future_seconds = max_future_seconds

    def check(self, signal, now=None):
        timestamp = datetime.fromisoformat(signal.timestamp)

        if timestamp.tzinfo is None:
            raise StaleSignalError("Timezone-aware timestamp required")

        if now is None:
            now = datetime.now(timezone.utc)

        if now.tzinfo is None:
            raise ValueError("Current time must include timezone")

        age_seconds = (now - timestamp).total_seconds()

        if age_seconds > self.max_age_seconds:
            raise StaleSignalError("TradingView signal is too old")

        if age_seconds < -self.max_future_seconds:
            raise StaleSignalError("TradingView signal is future-dated")