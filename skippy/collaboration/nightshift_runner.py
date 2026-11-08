"""
SKIPPY AI Collaboration Bridge — NIGHTSHIFT-01 Bounded Offline Runner.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Thin orchestration layer that drives one simulated Chief assignment,
one simulated Jason result, one simulated SKIPPY review, and at most
one correction cycle. Produces a durable, restart-safe, clearly
synthetic morning report.

Does not replace CollaborationOrchestrator. Does not create a parallel
authority. Does not claim cross-database atomicity.
execution_enabled remains False.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skippy.collaboration.checkpoint_ledger import (
    CheckpointLedger,
    CheckpointState,
    EventType,
    LedgerError,
)
from skippy.collaboration.coordinated_halt_gate import CoordinatedHaltGate
from skippy.collaboration.orchestrator import CollaborationOrchestrator
from skippy.collaboration.protocol import (
    CollaborationProtocol,
    ProtocolError,
)


class NightshiftError(RuntimeError):
    """Raised when the nightshift runner cannot complete safely."""


@dataclass(frozen=True)
class NightshiftReport:
    """Durable, read-only, synthetic morning report."""

    assignment_id: str
    project_id: str
    task_id: str
    checkpoint_id: str
    state: str
    outcome: str
    correction_count: int
    local_tests_reported: int | None
    source_verified: bool
    ci_verified: bool
    refusal_reason: str | None
    pending_chief_decisions: list[str]
    synthetic: bool = True
    execution_enabled: bool = field(default=False, init=False)
    provenance: str = "synthetic-fixture"


class NightshiftRunner:
    """
    Bounded offline one-task handoff runner.

    Processes exactly one simulated assignment, one Jason result,
    one SKIPPY review, and at most one correction. Fail-closed.
    """

    execution_enabled = False
    MAX_STEPS = 8
    MAX_CORRECTIONS = 1

    def __init__(
        self,
        protocol: CollaborationProtocol,
        ledger: CheckpointLedger,
        gate: CoordinatedHaltGate | None = None,
        clock=None,
    ):
        if gate is not None and type(gate) is not CoordinatedHaltGate:
            raise NightshiftError("Invalid coordinated guard type")
        self.protocol = protocol
        self.ledger = ledger
        self.gate = gate
        self.orchestrator = CollaborationOrchestrator(protocol)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._steps = 0
        self._corrections = 0
        self._started = False

    def _now(self) -> datetime:
        return self._clock()

    def _check_prerequisites(self) -> str | None:
        """Return refusal reason or None if ready."""
        if self.gate is None:
            return "Missing coordinated guard"
        try:
            result = self.gate.check()
            if not result.allowed or result.execution_enabled is not False:
                return f"Coordinated guard rejected: {result.reason}"
            if result.coordinated_status == "HALTED":
                return "Coordinated HALTED"
            if result.coordinated_status == "UNKNOWN":
                return "Coordinated UNKNOWN"
        except Exception:
            return "Coordinated guard evaluation failed"
        if self.protocol.is_halted:
            return "Protocol is halted"
        return None

    def run(
        self,
        *,
        assignment_id: str,
        project_id: str,
        task_id: str,
        checkpoint_id: str,
        objective: str,
        expected_revision: int = 0,
        force_inadequate: bool = False,
    ) -> NightshiftReport:
        """
        Execute one bounded synthetic handoff.

        Returns a durable report. Does not retry, self-heal, or enable
        execution. Fail-closed on any prerequisite or transition failure.
        """
        if self._started:
            return self._refuse(assignment_id, project_id, task_id, checkpoint_id, "Duplicate runner instance")

        refusal = self._check_prerequisites()
        if refusal:
            return self._refuse(assignment_id, project_id, task_id, checkpoint_id, refusal)

        self._started = True
        now = self._now()

        try:
            # Record assignment
            self.ledger.record_event(
                event_type=EventType.ASSIGNMENT_CREATED,
                project_id=project_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                actor="chief",
                payload={"assignment_id": assignment_id, "objective": objective, "synthetic": True},
                message_id=f"assign-{assignment_id}",
                idempotency_key=f"idem-assign-{assignment_id}",
                expected_revision=expected_revision,
                now=now,
            )
            self._steps += 1

            # Drive orchestrator for the simulated workflow
            report = self.orchestrator.run(task_id, objective, force_correction=force_inadequate)
            self._steps += 1

            # Record implementation submitted (synthetic)
            self.ledger.record_event(
                event_type=EventType.IMPLEMENTATION_SUBMITTED,
                project_id=project_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                actor="jason",
                payload={
                    "sha": "synthetic-local",
                    "local_tests": 0,
                    "synthetic": True,
                    "assignment_id": assignment_id,
                },
                message_id=f"impl-{assignment_id}",
                idempotency_key=f"idem-impl-{assignment_id}",
                expected_revision=expected_revision + 1,
                now=now,
            )
            self._steps += 1

            # Record review
            self.ledger.record_event(
                event_type=EventType.REVIEW_RECORDED,
                project_id=project_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                actor="skippy",
                payload={
                    "sha": "synthetic-local",
                    "source_verified": True,
                    "synthetic": True,
                    "assignment_id": assignment_id,
                },
                message_id=f"review-{assignment_id}",
                idempotency_key=f"idem-review-{assignment_id}",
                expected_revision=expected_revision + 2,
                now=now,
            )
            self._steps += 1

            if force_inadequate and self._corrections < self.MAX_CORRECTIONS:
                self.ledger.record_event(
                    event_type=EventType.CORRECTION_REQUESTED,
                    project_id=project_id,
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    actor="skippy",
                    payload={"findings": "synthetic correction", "synthetic": True},
                    message_id=f"corr-{assignment_id}",
                    idempotency_key=f"idem-corr-{assignment_id}",
                    expected_revision=expected_revision + 3,
                    now=now,
                )
                self._corrections += 1
                self._steps += 1

            status = self.ledger.status(project_id, task_id)
            return NightshiftReport(
                assignment_id=assignment_id,
                project_id=project_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                state=status.state.value if status else "UNCERTAIN",
                outcome="synthetic-complete",
                correction_count=self._corrections,
                local_tests_reported=status.local_tests_reported if status else None,
                source_verified=bool(status and status.state == CheckpointState.SOURCE_VERIFIED),
                ci_verified=False,  # never claim independent CI
                refusal_reason=None,
                pending_chief_decisions=["PR authorization", "merge authorization"],
            )

        except (LedgerError, ProtocolError, Exception) as exc:
            return self._refuse(
                assignment_id, project_id, task_id, checkpoint_id, str(exc)
            )

    def _refuse(
        self,
        assignment_id: str,
        project_id: str,
        task_id: str,
        checkpoint_id: str,
        reason: str,
    ) -> NightshiftReport:
        return NightshiftReport(
            assignment_id=assignment_id,
            project_id=project_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            state="REFUSED",
            outcome="fail-closed",
            correction_count=self._corrections,
            local_tests_reported=None,
            source_verified=False,
            ci_verified=False,
            refusal_reason=reason,
            pending_chief_decisions=[],
        )

    def report(self, project_id: str, task_id: str) -> NightshiftReport | None:
        """Reconstruct report from durable ledger state. Does not re-run agents."""
        status = self.ledger.status(project_id, task_id)
        if status is None:
            return None
        return NightshiftReport(
            assignment_id="reconstructed",
            project_id=project_id,
            task_id=task_id,
            checkpoint_id=status.checkpoint_id,
            state=status.state.value,
            outcome="reconstructed-from-ledger",
            correction_count=0,
            local_tests_reported=status.local_tests_reported,
            source_verified=status.state == CheckpointState.SOURCE_VERIFIED,
            ci_verified=status.ci_verified,
            refusal_reason=None,
            pending_chief_decisions=["PR authorization", "merge authorization"],
        )
