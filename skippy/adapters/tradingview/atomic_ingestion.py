"""
SKIPPY AI TRADER - PHASE 8

Atomic TradingView Signal Ingestion.

LOCAL SIMULATION ONLY.
No HTTP listener, broker connections, or trade execution.
"""

from pathlib import Path

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.deduplicator import (
    DuplicateSignalError,
    SignalDeduplicator,
)
from skippy.adapters.tradingview.freshness import SignalFreshnessValidator
from skippy.adapters.tradingview.receiver import TRADINGVIEW_SIGNAL_VALIDATED
from skippy.adapters.tradingview.security_gateway import WebhookSecurityGateway
from skippy.adapters.tradingview.validator import validate_signal
from skippy.kernel.event_bus import EventBus

from dataclasses import asdict


class AtomicTradingViewIngestion:
    def __init__(
        self,
        event_bus: EventBus,
        secret: str,
        database_path: str | Path,
        max_payload_bytes: int = 8192,
    ):
        if database_path is None:
            raise ValueError(
                "Atomic ingestion requires persistent database storage"
            )

        self.event_bus = event_bus

        self.gateway = WebhookSecurityGateway(
            secret=secret,
            max_payload_bytes=max_payload_bytes,
        )

        self.freshness = SignalFreshnessValidator()

        self.deduplicator = SignalDeduplicator()

        self.registry = AtomicSignalRegistry(database_path)

    def ingest(self, body: bytes, provided_token: str) -> dict:
        """
        Authenticate, validate, register, and publish a signal.

        Registration is atomic within SQLite.
        Event publication is not part of the SQLite transaction.
        """

        payload = self.gateway.validate(
            body,
            provided_token,
        )

        validated = validate_signal(payload)

        self.freshness.check(validated)

        fingerprint = self.deduplicator.fingerprint(validated)

        registered = self.registry.register(fingerprint)

        if not registered:
            raise DuplicateSignalError(
                "Atomic duplicate TradingView signal rejected"
            )

        signal_data = asdict(validated)

        self.event_bus.publish(
            TRADINGVIEW_SIGNAL_VALIDATED,
            signal_data,
        )

        return signal_data