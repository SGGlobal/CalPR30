"""
SKIPPY AI TRADER — PHASE 8

Local Webhook Security Gateway

SIMULATION ONLY.
No HTTP listener, external connections, or trade execution.
"""

import hmac
import json


class WebhookSecurityError(ValueError):
    """Raised when a simulated webhook request is rejected."""


class WebhookSecurityGateway:
    def __init__(
        self,
        secret: str,
        max_payload_bytes: int = 8192,
    ):
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("Authentication secret must contain at least 32 characters")

        if (
            isinstance(max_payload_bytes, bool)
            or not isinstance(max_payload_bytes, int)
            or max_payload_bytes <= 0
        ):
            raise ValueError("Payload limit must be a positive integer")

        self._secret = secret.encode("utf-8")
        self.max_payload_bytes = max_payload_bytes

    def validate(self, body: bytes, provided_token: str) -> dict:
        """
        Authenticate and parse locally supplied webhook data.

        This does not authenticate TradingView itself.
        """

        if not isinstance(body, bytes):
            raise WebhookSecurityError("Webhook body must be bytes")

        if len(body) > self.max_payload_bytes:
            raise WebhookSecurityError("Webhook payload exceeds size limit")

        if not isinstance(provided_token, str):
            raise WebhookSecurityError("Authentication token is invalid")

        if not hmac.compare_digest(
            provided_token.encode("utf-8"),
            self._secret,
        ):
            raise WebhookSecurityError("Webhook authentication failed")

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebhookSecurityError("Invalid JSON payload") from exc

        if not isinstance(payload, dict):
            raise WebhookSecurityError("JSON payload must be an object")

        return payload