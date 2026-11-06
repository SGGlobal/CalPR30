"""
SKIPPY AI Collaboration Bridge — Local Orchestrator.

LOCAL SYNTHETIC ONLY.
Coordinates simulated Skyppy and Jason through CollaborationProtocol.
No external APIs, no repository writes, no trading execution.

Delivery sequence for every message:
1. Validate recipient.
2. Durably accept.
3. Dispatch to the designated simulated handler.
4. Acknowledge only after successful handling.

recover() classifies progress from persisted records. It does not
automatically continue a partial workflow.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from skippy.collaboration.agents import SimulatedJason, SimulatedSkyppy
from skippy.collaboration.protocol import (
    CollaborationProtocol,
    Message,
    MessageType,
    ProtocolError,
    TaskState,
)
from skippy.collaboration.router import LocalRouter


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator cannot complete a workflow."""


class RecoveryBlocked(OrchestratorError):
    """Raised when safe continuation cannot be established."""


class CollaborationOrchestrator:
    """
    Drives a complete synthetic collaboration workflow.

    All task transitions go through CollaborationProtocol.accept().
    """

    def __init__(self, protocol: CollaborationProtocol):
        self.protocol = protocol
        self.router = LocalRouter(protocol)
        self.skyppy = SimulatedSkyppy()
        self.jason = SimulatedJason()
        self._register_handlers()

    def _register_handlers(self) -> None:
        def _skyppy(message: Message) -> None:
            # Deterministic no-op handler; agent work is explicit in run().
            return None

        def _jason(message: Message) -> None:
            return None

        def _bridge(message: Message) -> None:
            return None

        def _chief(message: Message) -> None:
            return None

        self.router.register("skyppy", _skyppy)
        self.router.register("jason", _jason)
        self.router.register("bridge", _bridge)
        self.router.register("chief", _chief)

    def _message(
        self,
        task_id: str,
        correlation_id: str,
        sender: str,
        recipient: str,
        message_type: MessageType,
        payload: dict[str, Any],
        parent: str | None,
        key_suffix: str,
        now: datetime,
    ) -> Message:
        return Message(
            message_id=str(uuid.uuid4()),
            idempotency_key=f"{key_suffix}-{task_id}-{uuid.uuid4().hex[:8]}",
            correlation_id=correlation_id,
            project_id="QLA",
            task_id=task_id,
            sender=sender,
            recipient=recipient,
            message_type=message_type,
            timestamp=now,
            payload=payload,
            parent_message_id=parent,
        )

    def _send(self, message: Message, now: datetime) -> Message:
        """
        Accept, then dispatch, then acknowledge.

        If acceptance fails, the message is not acknowledged.
        If dispatch or handling fails, the message remains accepted
        but unacknowledged.
        """

        if self.protocol.is_halted and message.message_type is not MessageType.HALT:
            raise ProtocolError("Protocol is halted; new work rejected")

        self.router.validate_recipient(message)

        accepted = self.protocol.accept(message, now=now)
        if not accepted:
            # Valid duplicate delivery. Do not re-acknowledge and do not
            # reprocess. If it was never acknowledged, leave it that way.
            return message

        try:
            outcome = self.router.dispatch(message)
        except Exception:
            # Accepted but not acknowledged.
            raise

        if outcome == "duplicate":
            return message

        self.protocol.acknowledge(message.message_id)
        return message

    def _task_messages(self, task_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.protocol.database_path) as connection:
            rows = connection.execute(
                """
                SELECT message_id, message_type, correlation_id,
                       sender, recipient, payload_json
                FROM messages
                WHERE task_id = ?
                ORDER BY rowid
                """,
                (task_id,),
            ).fetchall()

        return [
            {
                "message_id": row[0],
                "message_type": row[1],
                "correlation_id": row[2],
                "sender": row[3],
                "recipient": row[4],
                "payload": json.loads(row[5]),
            }
            for row in rows
        ]

    def recover(self, task_id: str) -> dict[str, Any]:
        """
        Classify progress from persisted records.

        This is recovery classification, not automatic resumption.
        If any message belonging to the task is unacknowledged, the
        task is not classified as safely resumable.
        """

        state = self.protocol.get_state(task_id)
        if state is None:
            return {"task_id": task_id, "recovery": "not_found"}

        if state is TaskState.COMPLETE:
            return {
                "task_id": task_id,
                "recovery": "complete_not_replayed",
                "status": state.value,
            }

        messages = self._task_messages(task_id)
        if not messages:
            raise RecoveryBlocked("No messages for existing task")

        task_ids = {msg["message_id"] for msg in messages}
        outstanding = [
            mid
            for mid in self.protocol.unacknowledged()
            if mid in task_ids
        ]

        if outstanding:
            return {
                "task_id": task_id,
                "recovery": "uncertain",
                "status": state.value,
                "outstanding": outstanding,
                "correlation_id": messages[-1]["correlation_id"],
                "reason": "One or more messages have unresolved handling",
            }

        resumable = {
            TaskState.ASSIGNED,
            TaskState.ACKNOWLEDGED,
            TaskState.IN_PROGRESS,
            TaskState.RESULT_SUBMITTED,
            TaskState.UNDER_REVIEW,
            TaskState.CORRECTION_REQUESTED,
        }

        if state in resumable:
            return {
                "task_id": task_id,
                "recovery": "resumable",
                "status": state.value,
                "last_message_id": messages[-1]["message_id"],
                "correlation_id": messages[-1]["correlation_id"],
            }

        return {
            "task_id": task_id,
            "recovery": "blocked",
            "status": state.value,
            "reason": "State does not permit safe continuation",
        }

    def run(
        self,
        task_id: str,
        objective: str,
        force_correction: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Execute a full synthetic workflow.

        Returns a summary dict. Does not authorize real actions.
        """

        if self.protocol.is_halted:
            raise ProtocolError("Protocol is halted; new work rejected")

        existing = self.protocol.get_state(task_id)
        if existing is TaskState.COMPLETE:
            raise ProtocolError("Completed task is not replayed")
        if existing is not None:
            raise ProtocolError("Task already exists; use recover()")

        now = now or datetime.now(timezone.utc)
        correlation = str(uuid.uuid4())

        assignment_payload = self.skyppy.assignment(objective)
        if force_correction:
            assignment_payload = dict(assignment_payload)
            assignment_payload["force_correction"] = True

        assignment = self._send(
            self._message(
                task_id, correlation, "skyppy", "jason",
                MessageType.TASK_ASSIGNMENT, assignment_payload, None,
                "assign", now,
            ),
            now,
        )

        ack = self._send(
            self._message(
                task_id, correlation, "jason", "skyppy",
                MessageType.ACK, {"received": True}, assignment.message_id,
                "ack", now,
            ),
            now,
        )

        start = self._send(
            self._message(
                task_id, correlation, "jason", "bridge",
                MessageType.START, {"started": True}, ack.message_id,
                "start", now,
            ),
            now,
        )

        plan = self.jason.plan(assignment_payload, correction=False)
        result = self._send(
            self._message(
                task_id, correlation, "jason", "skyppy",
                MessageType.RESULT, plan, start.message_id,
                "result", now,
            ),
            now,
        )

        review_payload = self.skyppy.review(plan)
        review = self._send(
            self._message(
                task_id, correlation, "skyppy", "jason",
                MessageType.REVIEW, review_payload, result.message_id,
                "review", now,
            ),
            now,
        )

        if review_payload["decision"] == "correction_required":
            correction = self._send(
                self._message(
                    task_id, correlation, "skyppy", "jason",
                    MessageType.CORRECTION,
                    {"reason": review_payload["reason"]},
                    review.message_id,
                    "correction", now,
                ),
                now,
            )

            restart = self._send(
                self._message(
                    task_id, correlation, "jason", "bridge",
                    MessageType.START, {"restarted": True},
                    correction.message_id,
                    "restart", now,
                ),
                now,
            )

            corrected = self.jason.plan(assignment_payload, correction=True)
            result = self._send(
                self._message(
                    task_id, correlation, "jason", "skyppy",
                    MessageType.RESULT, corrected, restart.message_id,
                    "result-corrected", now,
                ),
                now,
            )

            review_payload = self.skyppy.review(corrected)
            review = self._send(
                self._message(
                    task_id, correlation, "skyppy", "jason",
                    MessageType.REVIEW, review_payload, result.message_id,
                    "review-corrected", now,
                ),
                now,
            )

        if review_payload["decision"] != "approved":
            raise OrchestratorError(
                "Correction loop exhausted; task not completed"
            )

        self._send(
            self._message(
                task_id, correlation, "skyppy", "bridge",
                MessageType.COMPLETION,
                {"outcome": "approved"},
                review.message_id,
                "complete", now,
            ),
            now,
        )

        return self.report(task_id)

    def report(self, task_id: str) -> dict[str, Any]:
        """
        Read-only mission report derived from persisted records.

        Accurate after orchestrator restart. No private payloads.
        """

        state = self.protocol.get_state(task_id)
        messages = self._task_messages(task_id)

        corrections = sum(
            1 for msg in messages if msg["message_type"] == "CORRECTION"
        )

        reviews = [
            msg for msg in messages if msg["message_type"] == "REVIEW"
        ]
        outcome = None
        if reviews:
            outcome = reviews[-1]["payload"].get("decision")

        task_ids = {msg["message_id"] for msg in messages}
        unacked = [
            mid
            for mid in self.protocol.unacknowledged()
            if mid in task_ids
        ]

        return {
            "task_id": task_id,
            "status": state.value if state else None,
            "assigned_agent": "jason",
            "message_count": len(messages),
            "corrections": corrections,
            "outcome": outcome,
            "unacknowledged": unacked,
            "halted": self.protocol.is_halted,
        }

    def to_json(self, task_id: str) -> str:
        return json.dumps(self.report(task_id), indent=2, sort_keys=True)
