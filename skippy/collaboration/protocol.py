"""
SKIPPY AI Collaboration Bridge — Communication Protocol & State Machine.

LOCAL SYNTHETIC ONLY.
No external AI APIs, no repository writes, no trading execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


PROTOCOL_VERSION = "1.0"


class ProtocolError(ValueError):
    """Raised when a message or transition is invalid."""


class TaskState(str, Enum):
    ASSIGNED = "ASSIGNED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    RESULT_SUBMITTED = "RESULT_SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    CORRECTION_REQUESTED = "CORRECTION_REQUESTED"
    COMPLETE = "COMPLETE"
    HALTED = "HALTED"
    UNCERTAIN = "UNCERTAIN"


class MessageType(str, Enum):
    TASK_ASSIGNMENT = "TASK_ASSIGNMENT"
    ACK = "ACK"
    RESULT = "RESULT"
    REVIEW = "REVIEW"
    CORRECTION = "CORRECTION"
    COMPLETION = "COMPLETION"
    HALT = "HALT"
    ERROR = "ERROR"


# Allowed transitions: current -> set of next states
ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.ASSIGNED: {
        TaskState.ACKNOWLEDGED,
        TaskState.HALTED,
    },
    TaskState.ACKNOWLEDGED: {
        TaskState.IN_PROGRESS,
        TaskState.HALTED,
    },
    TaskState.IN_PROGRESS: {
        TaskState.RESULT_SUBMITTED,
        TaskState.UNCERTAIN,
        TaskState.HALTED,
    },
    TaskState.RESULT_SUBMITTED: {
        TaskState.UNDER_REVIEW,
        TaskState.HALTED,
    },
    TaskState.UNDER_REVIEW: {
        TaskState.COMPLETE,
        TaskState.CORRECTION_REQUESTED,
        TaskState.HALTED,
    },
    TaskState.CORRECTION_REQUESTED: {
        TaskState.IN_PROGRESS,
        TaskState.HALTED,
    },
    TaskState.COMPLETE: set(),
    TaskState.HALTED: set(),
    TaskState.UNCERTAIN: {
        TaskState.IN_PROGRESS,  # manual reconciliation only
        TaskState.HALTED,
    },
}


MESSAGE_TO_STATE = {
    MessageType.TASK_ASSIGNMENT: TaskState.ASSIGNED,
    MessageType.ACK: TaskState.ACKNOWLEDGED,
    MessageType.RESULT: TaskState.RESULT_SUBMITTED,
    MessageType.REVIEW: TaskState.UNDER_REVIEW,
    MessageType.CORRECTION: TaskState.CORRECTION_REQUESTED,
    MessageType.COMPLETION: TaskState.COMPLETE,
    MessageType.HALT: TaskState.HALTED,
}


@dataclass(frozen=True)
class Message:
    """Versioned collaboration message."""

    message_id: str
    idempotency_key: str
    correlation_id: str
    project_id: str
    task_id: str
    sender: str
    recipient: str
    message_type: MessageType
    timestamp: datetime
    authorization_status: str = "NONE"
    payload: dict[str, Any] = field(default_factory=dict)
    parent_message_id: str | None = None
    protocol_version: str = PROTOCOL_VERSION

    def __post_init__(self):
        if not self.message_id:
            raise ProtocolError("message_id is required")
        if not self.idempotency_key:
            raise ProtocolError("idempotency_key is required")
        if not self.correlation_id:
            raise ProtocolError("correlation_id is required")
        if not isinstance(self.message_type, MessageType):
            raise ProtocolError("invalid message_type")
        if self.authorization_status not in {
            "NONE",
            "PENDING",
            "APPROVED",
            "DENIED",
            "EXPIRED",
        }:
            raise ProtocolError("invalid authorization_status")


@dataclass
class ApprovalRecord:
    """Separate from AI messages. Never derived from message text."""

    approval_id: str
    chief_identity: str
    action: str
    task_id: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass
class OutboundRequest:
    """Tracked separately from internal message delivery."""

    request_id: str
    task_id: str
    status: str  # PENDING, CONFIRMED, UNCERTAIN, FAILED
    created_at: datetime
    attempts: int = 0


class CollaborationProtocol:
    """
    Local, in-memory collaboration protocol.

    No external network calls. Synthetic agents only.
    """

    def __init__(
        self,
        max_messages_per_task: int = 10,
        max_retries: int = 3,
        message_ttl_seconds: int = 86400,
    ):
        self.max_messages_per_task = max_messages_per_task
        self.max_retries = max_retries
        self.message_ttl = timedelta(seconds=message_ttl_seconds)

        self._messages: dict[str, Message] = {}
        self._idempotency: dict[str, str] = {}  # key -> message_id
        self._tasks: dict[str, TaskState] = {}
        self._task_message_count: dict[str, int] = {}
        self._acks: set[str] = set()
        self._approvals: dict[str, ApprovalRecord] = {}
        self._outbound: dict[str, OutboundRequest] = {}
        self._halted: bool = False

    @property
    def is_halted(self) -> bool:
        return self._halted

    def halt(self) -> None:
        """Global HALT. Prevents new work."""
        self._halted = True

    def get_state(self, task_id: str) -> TaskState | None:
        return self._tasks.get(task_id)

    def accept(self, message: Message, now: datetime | None = None) -> bool:
        """
        Accept a message if valid.

        Returns True if newly accepted.
        Returns False if it is a duplicate of an already-accepted message.
        Raises ProtocolError on invalid content, conflicting idempotency,
        invalid transition, expiration, bound exceeded, or HALT.
        """

        now = now or datetime.now(timezone.utc)

        if self._halted and message.message_type is not MessageType.HALT:
            raise ProtocolError("Protocol is halted; new work rejected")

        # Expiration
        if now - message.timestamp > self.message_ttl:
            raise ProtocolError("Message expired")

        # Duplicate message_id
        if message.message_id in self._messages:
            return False

        # Idempotency
        if message.idempotency_key in self._idempotency:
            existing_id = self._idempotency[message.idempotency_key]
            existing = self._messages[existing_id]
            if existing.payload != message.payload:
                raise ProtocolError(
                    "Conflicting payload for existing idempotency key"
                )
            # Same key, same payload → duplicate delivery
            return False

        # Bound
        count = self._task_message_count.get(message.task_id, 0)
        if count >= self.max_messages_per_task:
            raise ProtocolError("Maximum messages per task exceeded")

        # State transition
        current = self._tasks.get(message.task_id)
        target = MESSAGE_TO_STATE.get(message.message_type)

        if message.message_type is MessageType.TASK_ASSIGNMENT:
            if current is not None:
                raise ProtocolError("Task already exists")
            target = TaskState.ASSIGNED
        elif target is None:
            raise ProtocolError("Unsupported message type for transition")
        else:
            if current is None:
                raise ProtocolError("Task does not exist")
            if target not in ALLOWED_TRANSITIONS.get(current, set()):
                raise ProtocolError(
                    f"Invalid transition: {current.value} -> {target.value}"
                )

        # Accept
        self._messages[message.message_id] = message
        self._idempotency[message.idempotency_key] = message.message_id
        self._tasks[message.task_id] = target
        self._task_message_count[message.task_id] = count + 1

        return True

    def acknowledge(self, message_id: str) -> None:
        """Record durable acknowledgment of an accepted message."""
        if message_id not in self._messages:
            raise ProtocolError("Cannot acknowledge unknown message")
        self._acks.add(message_id)

    def is_acknowledged(self, message_id: str) -> bool:
        return message_id in self._acks

    def unacknowledged(self) -> list[Message]:
        """Messages accepted but not yet acknowledged."""
        return [
            msg
            for mid, msg in self._messages.items()
            if mid not in self._acks
        ]

    def record_approval(self, approval: ApprovalRecord) -> None:
        """Store a Chief authorization. Separate from AI messages."""
        if not approval.chief_identity:
            raise ProtocolError("Approval requires authenticated Chief identity")
        if approval.approval_id in self._approvals:
            raise ProtocolError("Duplicate approval id")
        self._approvals[approval.approval_id] = approval

    def consume_approval(
        self,
        approval_id: str,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """
        Atomically consume an approval.

        Raises if missing, expired, or already consumed.
        """
        now = now or datetime.now(timezone.utc)
        approval = self._approvals.get(approval_id)

        if approval is None:
            raise ProtocolError("Approval not found")
        if approval.consumed_at is not None:
            raise ProtocolError("Approval already consumed")
        if now > approval.expires_at:
            raise ProtocolError("Approval expired")

        approval.consumed_at = now
        return approval

    def track_outbound(
        self,
        task_id: str,
        status: str = "PENDING",
    ) -> OutboundRequest:
        """
        Track an outbound request separately from internal delivery.

        Uncertain outcomes are recorded but never automatically retried.
        """
        if status not in {"PENDING", "CONFIRMED", "UNCERTAIN", "FAILED"}:
            raise ProtocolError("Invalid outbound status")

        request = OutboundRequest(
            request_id=str(uuid.uuid4()),
            task_id=task_id,
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        self._outbound[request.request_id] = request
        return request

    def mark_outbound_uncertain(self, request_id: str) -> None:
        """
        Record an uncertain external outcome.

        Does not retry. Approval is not reused.
        """
        request = self._outbound.get(request_id)
        if request is None:
            raise ProtocolError("Unknown outbound request")
        request.status = "UNCERTAIN"
        request.attempts += 1

        # Reflect uncertainty on the task if it exists.
        if request.task_id in self._tasks:
            current = self._tasks[request.task_id]
            if TaskState.UNCERTAIN in ALLOWED_TRANSITIONS.get(current, set()):
                self._tasks[request.task_id] = TaskState.UNCERTAIN

    def simulate_round_trip(
        self,
        task_id: str,
        assignment_payload: dict,
        result_payload: dict,
        now: datetime | None = None,
    ) -> list[Message]:
        """
        Local synthetic Skyppy → Jason → Skyppy exchange.

        No external APIs. Returns the messages that were accepted.
        """
        now = now or datetime.now(timezone.utc)
        correlation = str(uuid.uuid4())

        assignment = Message(
            message_id=str(uuid.uuid4()),
            idempotency_key=f"assign-{task_id}",
            correlation_id=correlation,
            project_id="QLA",
            task_id=task_id,
            sender="skyppy",
            recipient="jason",
            message_type=MessageType.TASK_ASSIGNMENT,
            timestamp=now,
            payload=assignment_payload,
        )
        self.accept(assignment, now=now)
        self.acknowledge(assignment.message_id)

        ack = Message(
            message_id=str(uuid.uuid4()),
            idempotency_key=f"ack-{task_id}",
            correlation_id=correlation,
            project_id="QLA",
            task_id=task_id,
            sender="jason",
            recipient="skyppy",
            message_type=MessageType.ACK,
            timestamp=now,
            parent_message_id=assignment.message_id,
        )
        self.accept(ack, now=now)
        self.acknowledge(ack.message_id)

        # Move to IN_PROGRESS explicitly for the simulation.
        self._tasks[task_id] = TaskState.IN_PROGRESS

        result = Message(
            message_id=str(uuid.uuid4()),
            idempotency_key=f"result-{task_id}",
            correlation_id=correlation,
            project_id="QLA",
            task_id=task_id,
            sender="jason",
            recipient="skyppy",
            message_type=MessageType.RESULT,
            timestamp=now,
            payload=result_payload,
            parent_message_id=ack.message_id,
        )
        self.accept(result, now=now)
        self.acknowledge(result.message_id)

        review = Message(
            message_id=str(uuid.uuid4()),
            idempotency_key=f"review-{task_id}",
            correlation_id=correlation,
            project_id="QLA",
            task_id=task_id,
            sender="skyppy",
            recipient="jason",
            message_type=MessageType.REVIEW,
            timestamp=now,
            payload={"decision": "approved"},
            parent_message_id=result.message_id,
        )
        self.accept(review, now=now)
        self.acknowledge(review.message_id)

        return [assignment, ack, result, review]
