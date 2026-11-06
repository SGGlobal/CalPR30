"""
SKIPPY AI Collaboration Bridge — Local Message Router.

LOCAL SYNTHETIC ONLY.
No network listeners or public webhooks.

Delivery sequence:
1. Validate the recipient.
2. Caller durably accepts the message (protocol.accept).
3. Router dispatches to the simulated recipient handler.
4. Caller records the delivery acknowledgment only after success.

Transport acknowledgment is separate from task-level ACK messages.
"""

from __future__ import annotations

from skippy.collaboration.protocol import Message, ProtocolError


class RouterError(ValueError):
    """Raised when a message cannot be routed."""


class LocalRouter:
    """
    Minimal in-process router.

    Does not record delivery before acceptance. The orchestrator owns
    the accept-then-acknowledge sequence.
    """

    def __init__(self, protocol):
        self.protocol = protocol
        self._handled: set[str] = set()

    def validate_recipient(self, message: Message) -> None:
        if message.recipient not in {"skyppy", "jason", "chief", "bridge"}:
            raise RouterError("Invalid recipient")

    def dispatch(self, message: Message) -> str:
        """
        Dispatch an already-accepted message to its recipient handler.

        Returns "handled" on first successful dispatch, "duplicate" if
        this message_id was already handled. Does not acknowledge.
        """

        self.validate_recipient(message)

        if self.protocol.is_halted and message.message_type.value != "HALT":
            raise ProtocolError("Protocol is halted; routing rejected")

        if message.message_id in self._handled:
            return "duplicate"

        # Simulated handlers are pure; the orchestrator performs the
        # actual agent work. Recording here only prevents re-processing.
        self._handled.add(message.message_id)
        return "handled"

    def was_handled(self, message_id: str) -> bool:
        return message_id in self._handled
