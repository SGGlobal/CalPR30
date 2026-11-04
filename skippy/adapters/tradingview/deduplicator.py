"""
SKIPPY AI TRADER — PHASE 8

Local Duplicate Signal Detector

SIMULATION ONLY.
No broker connections or trade execution.
"""

import hashlib
import json

from dataclasses import asdict


class DuplicateSignalError(ValueError):
    """Raised when a duplicate signal is detected."""


class SignalDeduplicator:
    def __init__(self):
        self._seen = set()

    def fingerprint(self, signal):
        data = asdict(signal)

        serialized = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
        )

        return hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest()

    def check(self, signal):
        fingerprint = self.fingerprint(signal)

        if fingerprint in self._seen:
            raise DuplicateSignalError(
                "Duplicate TradingView signal rejected"
            )

        self._seen.add(fingerprint)

    def clear(self):
        self._seen.clear()