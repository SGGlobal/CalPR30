"""
SKIPPY AI Collaboration Bridge — Local Orchestrator.

LOCAL SYNTHETIC ONLY.
Coordinates simulated Skyppy and Jason through CollaborationProtocol.
No external APIs, no repository writes, no trading execution.
"""

from __future__ import annotations

import json
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


MAX_CORRECTIONS = 1


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator cannot complete a workflow."""


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
        self._corrections: dict[str, int] = {}
        self._outcomes: dict[str, str] = {}

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
        if self.protocol.is_halted:
            raise ProtocolError("Protocol is halted; new work rejected")

        delivery = self.router.deliver(message)
        if delivery == "duplicate":
            return message

        self.protocol.accept(message, now=now)
        self.protocol.acknowledge(message.message_id)
        return message

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

        corrections = 0

        if review_payload["decision"] == "correction_required":
            corrections = 1
            self._corrections[task_id] = corrections

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

            # Correction transitions to CORRECTION_REQUESTED.
            # Move back to IN_PROGRESS via START is not allowed from
            # CORRECTION_REQUESTED; the protocol allows
            # CORRECTION_REQUESTED -> IN_PROGRESS. We use a START-equivalent
            # by sending a message that maps to IN_PROGRESS. The protocol
            # maps START to IN_PROGRESS, and CORRECTION_REQUESTED allows
            # IN_PROGRESS. So we send START again? START is only valid from
            # ACKNOWLEDGED. We need a path. Looking at ALLOWED_TRANSITIONS:
            # CORRECTION_REQUESTED -> IN_PROGRESS.
            # MESSAGE_TO_STATE has no dedicated type for that except we can
            # only get IN_PROGRESS from START, which is only valid from
            # ACKNOWLEDGED. This is a protocol limitation.
            #
            # For the prototype we complete the correction by having Jason
            # submit a new RESULT. But RESULT is only valid from IN_PROGRESS.
            # So we must transition CORRECTION_REQUESTED -> IN_PROGRESS.
            #
            # The protocol does not expose a public message type that maps
            # to IN_PROGRESS from CORRECTION_REQUESTED except START, and
            # START is only accepted from ACKNOWLEDGED.
            #
            # Workaround used here: send a message of type START is invalid.
            # Instead we document that the orchestrator uses the existing
            # START type only from ACKNOWLEDGED, and for correction we
            # rely on the protocol allowing CORRECTION_REQUESTED -> IN_PROGRESS
            # if we had a message type. Since we don't, we complete via
            # a second result only after an explicit IN_PROGRESS transition
            # that the protocol already supports if we add nothing.
            #
            # Practical approach: after CORRECTION, the state is
            # CORRECTION_REQUESTED. The protocol allows transition to
            # IN_PROGRESS. We need a message type that maps to IN_PROGRESS
            # and is allowed from CORRECTION_REQUESTED. START maps to
            # IN_PROGRESS but is only valid from ACKNOWLEDGED in the
            # current MESSAGE_TO_STATE check... wait, the check is:
            # target not in ALLOWED_TRANSITIONS[current]. START's target
            # is IN_PROGRESS. From CORRECTION_REQUESTED, IN_PROGRESS is
            # allowed. So START from CORRECTION_REQUESTED is valid!
            #
            # Yes: MESSAGE_TO_STATE[START] = IN_PROGRESS, and
            # ALLOWED_TRANSITIONS[CORRECTION_REQUESTED] includes IN_PROGRESS.
            # So a second START is the correct public transition.

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
            self._outcomes[task_id] = "correction_exhausted"
            raise OrchestratorError(
                "Correction loop exhausted; task not completed"
            )

        completion = self._send(
            self._message(
                task_id, correlation, "skyppy", "bridge",
                MessageType.COMPLETION,
                {"outcome": "approved"},
                review.message_id,
                "complete", now,
            ),
            now,
        )

        self._outcomes[task_id] = "approved"
        self._corrections[task_id] = corrections

        return self.report(task_id)

    def report(self, task_id: str) -> dict[str, Any]:
        """Read-only mission report. No sensitive payloads."""

        state = self.protocol.get_state(task_id)
        unacked = self.protocol.unacknowledged()

        return {
            "task_id": task_id,
            "status": state.value if state else None,
            "assigned_agent": "jason",
            "corrections": self._corrections.get(task_id, 0),
            "outcome": self._outcomes.get(task_id),
            "unacknowledged": unacked,
            "halted": self.protocol.is_halted,
        }

    def to_json(self, task_id: str) -> str:
        return json.dumps(self.report(task_id), indent=2, sort_keys=True)
