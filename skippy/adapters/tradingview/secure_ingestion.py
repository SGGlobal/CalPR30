"""
SKIPPY AI TRADER — PHASE 8

Authenticated Local Signal Ingestion.

SIMULATION ONLY.
No HTTP listener, external connections, or trade execution.
"""

from pathlib import Path

from skippy.adapters.tradingview.receiver import TradingViewReceiver
from skippy.adapters.tradingview.security_gateway import WebhookSecurityGateway
from skippy.kernel.event_bus import EventBus


class SecureTradingViewIngestion:
    def __init__(
        self,
        event_bus: EventBus,
        secret: str,
        replay_database: str | Path,
        max_payload_bytes: int = 8192,
    ):
        if replay_database is None:
            raise ValueError(
                "Secure ingestion requires persistent replay protection"
            )

        self.gateway = WebhookSecurityGateway(
            secret=secret,
            max_payload_bytes=max_payload_bytes,
        )

        self.receiver = TradingViewReceiver(
            event_bus,
            replay_database=replay_database,
        )

    def ingest(self, body: bytes, provided_token: str) -> dict:
        """
        Authenticate and validate a simulated webhook request.

        A request must pass every security check before publication.
        """

        payload = self.gateway.validate(
            body,
            provided_token,
        )

        return self.receiver.receive(payload)