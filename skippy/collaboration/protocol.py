"""
SKIPPY AI Collaboration Bridge — Communication Protocol & State Machine.

LOCAL SYNTHETIC ONLY.
No external AI APIs, no repository writes, no trading execution.

Approval records in this step are synthetic only.
They do not constitute real human authorization.
Actual authorization execution remains disabled until Step 69E.

Identity rules:
* Message envelope identity (message_id): the complete immutable envelope
  must match for a repeated message_id to be treated as a valid duplicate.
  Fields: message_id, idempotency_key, correlation_id, project_id, task_id,
  sender, recipient, message_type, authorization_status, payload,
  parent_message_id, protocol_version, and canonical timestamp.
* Logical-operation identity (idempotency_key): the same key represents
  the same logical operation when project_id, task_id, sender, recipient,
  message_type, authorization_status, and payload match.
  Timestamp is intentionally excluded from operation identity so that
  redelivery of the same operation is recognized.
  Legitimate repeated reviews use a new message_id and a new idempotency_key.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any


PROTOCOL_VERSION = "1.0"

KNOWN_AGENTS = frozenset({"skyppy", "jason", "chief", "bridge"})


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
    START = "START"
    RESULT = "RESULT"
    REVIEW = "REVIEW"
    CORRECTION = "CORRECTION"
    COMPLETION = "COMPLETION"
    HALT = "HALT"
    ERROR = "ERROR"


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
        TaskState.IN_PROGRESS,
        TaskState.HALTED,
    },
}


MESSAGE_TO_STATE = {
    MessageType.TASK_ASSIGNMENT: TaskState.ASSIGNED,
    MessageType.ACK: TaskState.ACKNOWLEDGED,
    MessageType.START: TaskState.IN_PROGRESS,
    MessageType.RESULT: TaskState.RESULT_SUBMITTED,
    MessageType.REVIEW: TaskState.UNDER_REVIEW,
    MessageType.CORRECTION: TaskState.CORRECTION_REQUESTED,
    MessageType.COMPLETION: TaskState.COMPLETE,
    MessageType.HALT: TaskState.HALTED,
}


def _freeze_payload(value: Any) -> Any:
    """
    Produce an immutable canonical snapshot of a payload.

    Rejects non-string keys, non-finite numbers, and non-JSON values.
    Nested dicts become MappingProxyType; lists become tuples.
    """

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("Payload contains non-finite number")
        return value

    if isinstance(value, dict):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("Payload keys must be strings")
            frozen[key] = _freeze_payload(item)
        return MappingProxyType(frozen)

    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)

    if isinstance(value, (str, int, bool)) or value is None:
        return value

    raise ProtocolError("Payload is not JSON-serializable")


def _canonical_json(value: Any) -> str:
    """Deterministic JSON from an already-frozen or plain value."""

    def _to_plain(obj: Any) -> Any:
        if isinstance(obj, MappingProxyType):
            return {k: _to_plain(v) for k, v in obj.items()}
        if isinstance(obj, tuple):
            return [_to_plain(v) for v in obj]
        if isinstance(obj, dict):
            return {k: _to_plain(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_to_plain(v) for v in obj]
        return obj

    return json.dumps(
        _to_plain(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True)
class Message:
    """Versioned collaboration message. Immutable after creation."""

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
    payload: Any = field(default_factory=dict)
    parent_message_id: str | None = None
    protocol_version: str = PROTOCOL_VERSION
    # Internal canonical snapshot; set in __post_init__.
    _canonical_payload: str = field(default="", repr=False, compare=False)

    def __post_init__(self):
        if self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolError("Unsupported protocol version")

        if not isinstance(self.message_id, str) or not self.message_id:
            raise ProtocolError("message_id is required")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ProtocolError("idempotency_key is required")
        if not isinstance(self.correlation_id, str) or not self.correlation_id:
            raise ProtocolError("correlation_id is required")
        if not isinstance(self.project_id, str) or not self.project_id:
            raise ProtocolError("project_id is required")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ProtocolError("task_id is required")

        if self.sender not in KNOWN_AGENTS:
            raise ProtocolError("Unknown sender")
        if self.recipient not in KNOWN_AGENTS:
            raise ProtocolError("Unknown recipient")

        if not isinstance(self.message_type, MessageType):
            raise ProtocolError("invalid message_type")

        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ProtocolError("timestamp must be timezone-aware")

        if not isinstance(self.payload, dict):
            raise ProtocolError("payload must be a dictionary")

        frozen = _freeze_payload(self.payload)
        canonical = _canonical_json(frozen)

        # Replace the mutable payload with the frozen snapshot.
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "_canonical_payload", canonical)

        if self.authorization_status not in {
            "NONE",
            "PENDING",
            "APPROVED",
            "DENIED",
            "EXPIRED",
        }:
            raise ProtocolError("invalid authorization_status")

        if (
            self.sender in {"skyppy", "jason"}
            and self.authorization_status == "APPROVED"
        ):
            raise ProtocolError(
                "AI message cannot establish an approved authorization state"
            )

    def envelope_identity(self) -> str:
        """
        Canonical identity of the complete immutable message envelope.

        Includes the canonical timestamp. Used when a message_id is repeated.
        """
        return _canonical_json({
            "message_id": self.message_id,
            "idempotency_key": self.idempotency_key,
            "correlation_id": self.correlation_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "message_type": self.message_type.value,
            "authorization_status": self.authorization_status,
            "payload": self.payload,
            "parent_message_id": self.parent_message_id,
            "protocol_version": self.protocol_version,
            "timestamp": self.timestamp.isoformat(),
        })

    def operation_identity(self) -> str:
        """
        Canonical identity of the logical operation.

        Timestamp is excluded so redelivery of the same operation is
        recognized. A new review with a new key remains distinct.
        """
        return _canonical_json({
            "project_id": self.project_id,
            "task_id": self.task_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "message_type": self.message_type.value,
            "authorization_status": self.authorization_status,
            "payload": self.payload,
            "protocol_version": self.protocol_version,
        })

    @property
    def canonical_payload(self) -> str:
        """The immutable canonical JSON snapshot of the payload."""
        return self._canonical_payload


@dataclass
class ApprovalRecord:
    """
    Synthetic approval record.

    Separate from AI messages.
    An identity string is not real human authentication.
    Actual authorization execution remains disabled until Step 69E.
    """

    approval_id: str
    chief_identity: str
    action: str
    task_id: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass
class OutboundRequest:
    request_id: str
    task_id: str
    status: str
    created_at: datetime
    attempts: int = 0


class CollaborationProtocol:
    """
    Local collaboration protocol with minimal SQLite persistence.

    No external network calls. Synthetic agents only.
    """

    def __init__(
        self,
        database_path: str | Path,
        max_messages_per_task: int = 10,
        max_retries: int = 3,
        message_ttl_seconds: int = 86400,
    ):
        if str(database_path) == ":memory:":
            raise ValueError("Protocol requires a disk database")

        self.database_path = Path(database_path)
        self.max_messages_per_task = max_messages_per_task
        self.max_retries = max_retries
        self.message_ttl = timedelta(seconds=message_ttl_seconds)

        if not self.database_path.parent.is_dir():
            raise ProtocolError("Database directory does not exist")

        self._init_schema()
        self._refresh_halt()

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    def _init_schema(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    correlation_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    authorization_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    parent_message_id TEXT,
                    protocol_version TEXT NOT NULL,
                    envelope_identity TEXT NOT NULL,
                    operation_identity TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS acknowledgments (
                    message_id TEXT PRIMARY KEY
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_states (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    message_count INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_requests (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    chief_identity TEXT NOT NULL,
                    action TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )

    def _refresh_halt(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM control WHERE key = 'halted'"
            ).fetchone()
            self._halted = bool(row and row[0] == "1")
        return self._halted

    @property
    def is_halted(self) -> bool:
        return self._refresh_halt()

    def halt(self) -> None:
        """Global HALT. Survives reopen. No automatic resumption."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO control (key, value)
                VALUES ('halted', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
                """
            )
        self._halted = True

    def get_state(self, task_id: str) -> TaskState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM task_states WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return TaskState(row[0]) if row else None

    def accept(self, message: Message, now: datetime | None = None) -> bool:
        """
        Accept a message if valid.

        Returns True if newly accepted.
        Returns False if it is a valid duplicate.
        Raises ProtocolError on conflict, invalid transition, expiration,
        bound exceeded, or HALT.

        HALT is read from SQLite inside the acceptance transaction so a
        second instance cannot accept work after another instance halts.
        """

        now = now or datetime.now(timezone.utc)

        if now - message.timestamp > self.message_ttl:
            raise ProtocolError("Message expired")

        envelope = message.envelope_identity()
        operation = message.operation_identity()

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")

                halt_row = connection.execute(
                    "SELECT value FROM control WHERE key = 'halted'"
                ).fetchone()
                if halt_row and halt_row[0] == "1":
                    if message.message_type is not MessageType.HALT:
                        raise ProtocolError(
                            "Protocol is halted; new work rejected"
                        )

                existing_by_id = connection.execute(
                    """
                    SELECT envelope_identity
                    FROM messages
                    WHERE message_id = ?
                    """,
                    (message.message_id,),
                ).fetchone()

                if existing_by_id is not None:
                    if existing_by_id[0] != envelope:
                        raise ProtocolError(
                            "Conflicting content for existing message ID"
                        )
                    return False

                existing_by_key = connection.execute(
                    """
                    SELECT operation_identity
                    FROM messages
                    WHERE idempotency_key = ?
                    """,
                    (message.idempotency_key,),
                ).fetchone()

                if existing_by_key is not None:
                    if existing_by_key[0] != operation:
                        raise ProtocolError(
                            "Conflicting content for existing idempotency key"
                        )
                    return False

                count_row = connection.execute(
                    "SELECT message_count FROM task_states WHERE task_id = ?",
                    (message.task_id,),
                ).fetchone()
                count = count_row[0] if count_row else 0

                if count >= self.max_messages_per_task:
                    raise ProtocolError("Maximum messages per task exceeded")

                current_row = connection.execute(
                    "SELECT state FROM task_states WHERE task_id = ?",
                    (message.task_id,),
                ).fetchone()
                current = TaskState(current_row[0]) if current_row else None

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

                connection.execute(
                    """
                    INSERT INTO messages (
                        message_id, idempotency_key, correlation_id,
                        project_id, task_id, sender, recipient,
                        message_type, timestamp, authorization_status,
                        payload_json, parent_message_id, protocol_version,
                        envelope_identity, operation_identity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.message_id,
                        message.idempotency_key,
                        message.correlation_id,
                        message.project_id,
                        message.task_id,
                        message.sender,
                        message.recipient,
                        message.message_type.value,
                        message.timestamp.isoformat(),
                        message.authorization_status,
                        message.canonical_payload,
                        message.parent_message_id,
                        message.protocol_version,
                        envelope,
                        operation,
                    ),
                )

                connection.execute(
                    """
                    INSERT INTO task_states (task_id, state, message_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(task_id) DO UPDATE SET
                        state = excluded.state,
                        message_count = message_count + 1
                    """,
                    (message.task_id, target.value),
                )

                return True

        except sqlite3.Error as exc:
            raise ProtocolError(
                "Persistence failed; transaction rolled back"
            ) from exc

    def acknowledge(self, message_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise ProtocolError("Cannot acknowledge unknown message")
            connection.execute(
                "INSERT OR IGNORE INTO acknowledgments (message_id) VALUES (?)",
                (message_id,),
            )

    def is_acknowledged(self, message_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM acknowledgments WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def unacknowledged(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.message_id
                FROM messages AS m
                LEFT JOIN acknowledgments AS a
                    ON a.message_id = m.message_id
                WHERE a.message_id IS NULL
                """
            ).fetchall()
        return [row[0] for row in rows]

    def record_approval(self, approval: ApprovalRecord) -> None:
        """
        Store a synthetic approval.

        Does not authorize real actions. Execution remains disabled
        until Step 69E.
        """
        if not approval.chief_identity:
            raise ProtocolError("Approval requires an identity string")

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM approvals WHERE approval_id = ?",
                (approval.approval_id,),
            ).fetchone()
            if existing is not None:
                raise ProtocolError("Duplicate approval id")

            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, chief_identity, action, task_id,
                    issued_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.chief_identity,
                    approval.action,
                    approval.task_id,
                    approval.issued_at.isoformat(),
                    approval.expires_at.isoformat(),
                    None,
                ),
            )

    def consume_approval(
        self,
        approval_id: str,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """
        Consume a synthetic approval.

        This does not authorize real actions.
        """
        now = now or datetime.now(timezone.utc)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT chief_identity, action, task_id,
                       issued_at, expires_at, consumed_at
                FROM approvals
                WHERE approval_id = ?
                """,
                (approval_id,),
            ).fetchone()

            if row is None:
                raise ProtocolError("Approval not found")

            consumed_at = (
                datetime.fromisoformat(row[5]) if row[5] else None
            )
            expires_at = datetime.fromisoformat(row[4])

            if consumed_at is not None:
                raise ProtocolError("Approval already consumed")
            if now > expires_at:
                raise ProtocolError("Approval expired")

            connection.execute(
                """
                UPDATE approvals
                SET consumed_at = ?
                WHERE approval_id = ?
                  AND consumed_at IS NULL
                """,
                (now.isoformat(), approval_id),
            )

        return ApprovalRecord(
            approval_id=approval_id,
            chief_identity=row[0],
            action=row[1],
            task_id=row[2],
            issued_at=datetime.fromisoformat(row[3]),
            expires_at=expires_at,
            consumed_at=now,
        )

    def track_outbound(
        self,
        task_id: str,
        status: str = "PENDING",
    ) -> OutboundRequest:
        if status not in {"PENDING", "CONFIRMED", "UNCERTAIN", "FAILED"}:
            raise ProtocolError("Invalid outbound status")

        request = OutboundRequest(
            request_id=str(uuid.uuid4()),
            task_id=task_id,
            status=status,
            created_at=datetime.now(timezone.utc),
        )

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")

                halt_row = connection.execute(
                    "SELECT value FROM control WHERE key = 'halted'"
                ).fetchone()
                if halt_row and halt_row[0] == "1":
                    raise ProtocolError(
                        "Protocol is halted; outbound requests rejected"
                    )

                connection.execute(
                    """
                    INSERT INTO outbound_requests (
                        request_id, task_id, status, created_at, attempts
                    ) VALUES (?, ?, ?, ?, 0)
                    """,
                    (
                        request.request_id,
                        request.task_id,
                        request.status,
                        request.created_at.isoformat(),
                    ),
                )

        except sqlite3.Error as exc:
            raise ProtocolError(
                "Persistence failed; transaction rolled back"
            ) from exc

        return request

    def mark_outbound_uncertain(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, attempts FROM outbound_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()

            if row is None:
                raise ProtocolError("Unknown outbound request")

            connection.execute(
                """
                UPDATE outbound_requests
                SET status = 'UNCERTAIN', attempts = attempts + 1
                WHERE request_id = ?
                """,
                (request_id,),
            )

            task_id = row[0]
            current_row = connection.execute(
                "SELECT state FROM task_states WHERE task_id = ?",
                (task_id,),
            ).fetchone()

            if current_row is not None:
                current = TaskState(current_row[0])
                if TaskState.UNCERTAIN in ALLOWED_TRANSITIONS.get(current, set()):
                    connection.execute(
                        "UPDATE task_states SET state = ? WHERE task_id = ?",
                        (TaskState.UNCERTAIN.value, task_id),
                    )

    def simulate_round_trip(
        self,
        task_id: str,
        assignment_payload: dict,
        result_payload: dict,
        now: datetime | None = None,
    ) -> list[Message]:
        """
        Local synthetic Skyppy → Jason → Skyppy exchange.

        Uses only public accept() transitions. No direct state mutation.
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

        start = Message(
            message_id=str(uuid.uuid4()),
            idempotency_key=f"start-{task_id}",
            correlation_id=correlation,
            project_id="QLA",
            task_id=task_id,
            sender="jason",
            recipient="bridge",
            message_type=MessageType.START,
            timestamp=now,
            parent_message_id=ack.message_id,
        )
        self.accept(start, now=now)
        self.acknowledge(start.message_id)

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
            parent_message_id=start.message_id,
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

        return [assignment, ack, start, result, review]
