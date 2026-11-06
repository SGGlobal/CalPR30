"""
SKIPPY AI Collaboration Bridge — Local Message Router.

LOCAL SYNTHETIC ONLY.
No network listeners or public webhooks.
"""

from __future__ import annotations

from skippy.collaboration.protocol import Message, ProtocolError


class RouterError(ValueError):
    """Raised when a message cannot be routed."""


class LocalRouter:
    """
    Minimal in-process router.

    Distinguishes transport delivery from task-level ACK messages.
    Respects the protocol HALT flag before delivering new work.
    """

    def __init__(self, protocol):
        self.protocol = protocol
        self._delivered: set[str] = set()

    def deliver(self, message: Message) -> str:
        """
        Record a transport delivery.

        Returns "delivered" for a new delivery, "duplicate" if already
        delivered. Does not itself create a task-level ACK message.
        """

        if self.protocol.is_halted and message.message_type.value != "HALT":
            raise ProtocolError("Protocol is halted; routing rejected")

        if message.recipient not in {"skyppy", "jason", "chief", "bridge"}:
            raise RouterError("Invalid recipient")

        if message.message_id in self._delivered:
            return "duplicate"

        self._delivered.add(message.message_id)
        return "delivered"

    def was_delivered(self, message_id: str) -> bool:
        return message_id in self._delivered
