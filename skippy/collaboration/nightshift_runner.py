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
from datetime import datetime, timedelta, timezone
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
    TIMEOUT_SECONDS = 30

    def __init__(
        self,
        protocol: CollaborationProtocol,
        ledger: CheckpointLedger,
        gate: CoordinatedHaltGate | None = None,
        clock=None,
        max_steps: int | None = None,
        timeout_seconds: float | None = None,
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
        self._max_steps = max_steps if max_steps is not None else self.MAX_STEPS
        self._timeout = timeout_seconds if timeout_seconds is not None else self.TIMEOUT_SECONDS
        # Validate against fixed safe maximums
        if self._max_steps < 1 or self._max_steps > 32:
            raise NightshiftError("Step limit outside safe range")
        if self._timeout < 0.1 or self._timeout > 300:
            raise NightshiftError("Timeout outside safe range")
        self._start_time: datetime | None = None
        self._mono_start: float | None = None
        self._init_report_table()

    def _now(self) -> datetime:
        return self._clock()

    def _init_report_table(self):
        """Durable report storage in the ledger database."""
        with sqlite3.connect(self.ledger.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nightshift_reports (
                    assignment_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    correction_count INTEGER NOT NULL,
                    local_tests_reported INTEGER,
                    source_verified INTEGER NOT NULL,
                    ci_verified INTEGER NOT NULL,
                    refusal_reason TEXT,
                    pending_decisions TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )

    def _persist_report(self, report: NightshiftReport) -> None:
        """Persist a report. Never overwrite a completed assignment."""
        with sqlite3.connect(self.ledger.database_path) as connection:
            existing = connection.execute(
                "SELECT outcome FROM nightshift_reports WHERE assignment_id = ?",
                (report.assignment_id,),
            ).fetchone()
            if existing is not None:
                # Identical duplicate is a no-op; conflicting replay is rejected
                if existing[0] == report.outcome:
                    return  # preserve original
                raise NightshiftError("Conflicting replay of completed assignment")
            connection.execute(
                """
                INSERT INTO nightshift_reports (
                    assignment_id, project_id, task_id, checkpoint_id,
                    state, outcome, correction_count, local_tests_reported,
                    source_verified, ci_verified, refusal_reason,
                    pending_decisions, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.assignment_id,
                    report.project_id,
                    report.task_id,
                    report.checkpoint_id,
                    report.state,
                    report.outcome,
                    report.correction_count,
                    report.local_tests_reported,
                    1 if report.source_verified else 0,
                    1 if report.ci_verified else 0,
                    report.refusal_reason,
                    json.dumps(report.pending_chief_decisions),
                    self._now().isoformat(),
                ),
            )

    def _check_prerequisites(self) -> str | None:
        """Return refusal reason or None if ready. Uses existing admission controls."""
        if self.gate is None:
            return "Missing coordinated guard"
        # Must be the guard already bound to the protocol
        if getattr(self.protocol, "coordinated_guard", None) is not self.gate:
            return "Mismatched or unbound coordinated guard"
        if getattr(self.protocol, "coordinated_enforcement_required", False) is not True:
            return "Protocol enforcement not required"
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

    def _tick(self) -> None:
        """Enforce step budget and timeout. Fail closed if exceeded.

        Uses the injected clock for elapsed time. Synchronous orchestrator
        and ledger operations cannot be interrupted mid-call; the check
        occurs before each major step.
        """
        self._steps += 1
        if self._steps > self._max_steps:
            raise NightshiftError("Step budget exhausted")
        if self._start_time is not None:
            elapsed = (self._now() - self._start_time).total_seconds()
            if elapsed > self._timeout:
                raise NightshiftError("Timeout exceeded")

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
            report = self._refuse(assignment_id, project_id, task_id, checkpoint_id, "Duplicate runner instance")
            self._persist_report(report)
            return report

        refusal = self._check_prerequisites()
        if refusal:
            report = self._refuse(assignment_id, project_id, task_id, checkpoint_id, refusal)
            self._persist_report(report)
            return report

        self._started = True
        self._start_time = self._now()
        now = self._now()

        try:
            self._tick()
            # Record assignment only after guard verification
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

            self._tick()
            # Drive orchestrator and inspect actual result before recording success
            orch_result = self.orchestrator.run(task_id, objective, force_correction=force_inadequate)
            # Require expected task identity and terminal completion
            if not isinstance(orch_result, dict) or orch_result.get("task_id") != task_id:
                raise NightshiftError("Orchestrator result missing expected task identity")
            if orch_result.get("status") != "COMPLETE":
                # Partial or incomplete — report UNCERTAIN
                report = self._refuse(
                    assignment_id, project_id, task_id, checkpoint_id,
                    f"Orchestrator incomplete: {orch_result.get('status')}"
                )
                report = NightshiftReport(
                    assignment_id=assignment_id,
                    project_id=project_id,
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    state="UNCERTAIN",
                    outcome="uncertain",
                    correction_count=self._corrections,
                    local_tests_reported=None,
                    source_verified=False,
                    ci_verified=False,
                    refusal_reason=f"Orchestrator incomplete: {orch_result.get('status')}",
                    pending_chief_decisions=[],
                )
                self._persist_report(report)
                return report

            self._tick()
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

            self._tick()
            # Record initial review
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

            if force_inadequate and self._corrections < self.MAX_CORRECTIONS:
                self._tick()
                self.ledger.record_event(
                    event_type=EventType.CORRECTION_REQUESTED,
                    project_id=project_id,
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    actor="skippy",
                    payload={"findings": "synthetic correction", "synthetic": True},
                    message_id=f"corr-req-{assignment_id}",
                    idempotency_key=f"idem-corr-req-{assignment_id}",
                    expected_revision=expected_revision + 3,
                    now=now,
                )
                self._corrections += 1

                self._tick()
                # Correction submission
                self.ledger.record_event(
                    event_type=EventType.CORRECTION_SUBMITTED,
                    project_id=project_id,
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    actor="jason",
                    payload={
                        "sha": "synthetic-local-corrected",
                        "local_tests": 0,
                        "synthetic": True,
                        "assignment_id": assignment_id,
                    },
                    message_id=f"corr-sub-{assignment_id}",
                    idempotency_key=f"idem-corr-sub-{assignment_id}",
                    expected_revision=expected_revision + 4,
                    now=now,
                )

                self._tick()
                # Final review after correction
                self.ledger.record_event(
                    event_type=EventType.REVIEW_RECORDED,
                    project_id=project_id,
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    actor="skippy",
                    payload={
                        "sha": "synthetic-local-corrected",
                        "source_verified": True,
                        "synthetic": True,
                        "assignment_id": assignment_id,
                    },
                    message_id=f"review-final-{assignment_id}",
                    idempotency_key=f"idem-review-final-{assignment_id}",
                    expected_revision=expected_revision + 5,
                    now=now,
                )

            status = self.ledger.status(project_id, task_id)
            final_state = status.state.value if status else "UNCERTAIN"
            # Never report success while still in CORRECTION_REQUESTED
            if final_state == "CORRECTION_REQUESTED":
                report = self._refuse(
                    assignment_id, project_id, task_id, checkpoint_id,
                    "Ledger remains in CORRECTION_REQUESTED"
                )
            else:
                report = NightshiftReport(
                    assignment_id=assignment_id,
                    project_id=project_id,
                    task_id=task_id,
                    checkpoint_id=checkpoint_id,
                    state=final_state,
                    outcome="synthetic-complete",
                    correction_count=self._corrections,
                    local_tests_reported=status.local_tests_reported if status else None,
                    source_verified=bool(status and status.state == CheckpointState.SOURCE_VERIFIED),
                    ci_verified=False,
                    refusal_reason=None,
                    pending_chief_decisions=["PR authorization", "merge authorization"],
                )
            self._persist_report(report)
            return report

        except (LedgerError, ProtocolError, NightshiftError, Exception) as exc:
            report = self._refuse(
                assignment_id, project_id, task_id, checkpoint_id, str(exc)
            )
            try:
                self._persist_report(report)
            except Exception:
                pass  # best-effort persistence
            return report

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

    def report(self, assignment_id: str) -> NightshiftReport | None:
        """Reconstruct report from durable storage. Does not re-run agents."""
        with sqlite3.connect(self.ledger.database_path) as connection:
            row = connection.execute(
                """
                SELECT assignment_id, project_id, task_id, checkpoint_id,
                       state, outcome, correction_count, local_tests_reported,
                       source_verified, ci_verified, refusal_reason,
                       pending_decisions
                FROM nightshift_reports
                WHERE assignment_id = ?
                """,
                (assignment_id,),
            ).fetchone()
        if row is None:
            return None
        return NightshiftReport(
            assignment_id=row[0],
            project_id=row[1],
            task_id=row[2],
            checkpoint_id=row[3],
            state=row[4],
            outcome=row[5],
            correction_count=row[6],
            local_tests_reported=row[7],
            source_verified=bool(row[8]),
            ci_verified=bool(row[9]),
            refusal_reason=row[10],
            pending_chief_decisions=json.loads(row[11]),
        )
