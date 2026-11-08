"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A Offline Adapter Stub.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

External transport is disabled. This adapter accepts only synthetic
fixtures and records immutable audit receipts. It does not send,
retry, or enable execution.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from skippy.collaboration.authenticated_envelope import (
    AuthenticatedEnvelope,
    EnvelopeError,
    validate_envelope,
)
from skippy.collaboration.checkpoint_ledger import CheckpointLedger, EventType


class OfflineAdapterError(RuntimeError):
    """Raised when the offline adapter cannot process an envelope safely."""


class OfflineAuthenticatedAdapter:
    """
    Disabled-transport adapter for synthetic authenticated envelopes.

    Records immutable audit receipts. Fail-closed on any validation
    failure. No external network. No automatic retry. No catch-up after HALT.
    """

    execution_enabled = False
    TRANSPORT_ENABLED = False

    def __init__(self, ledger: CheckpointLedger, database_path: Path):
        self.ledger = ledger
        self.database_path = Path(database_path)
        self._seen_message_ids: set[str] = set()
        self._seen_idempotency_keys: set[str] = set()
        self._init_receipts()

    def _init_receipts(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT,
                    recorded_at TEXT NOT NULL
                )
                """
            )

    def _record_receipt(
        self,
        envelope: AuthenticatedEnvelope,
        outcome: str,
        reason: str | None,
        now: datetime,
    ) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO audit_receipts (
                    message_id, idempotency_key, project_id, task_id,
                    checkpoint_id, assignment_id, sender, outcome, reason, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.message_id,
                    envelope.idempotency_key,
                    envelope.project_id,
                    envelope.task_id,
                    envelope.checkpoint_id,
                    envelope.assignment_id,
                    envelope.sender,
                    outcome,
                    reason,
                    now.isoformat(),
                ),
            )

    def accept(
        self,
        envelope: AuthenticatedEnvelope,
        *,
        now: datetime | None = None,
        expected_sequence: int | None = None,
    ) -> str:
        """
        Validate and record an envelope. Returns 'accepted' or raises.

        Fail-closed. Does not dispatch external transport. Does not retry.
        """
        if self.TRANSPORT_ENABLED:
            raise OfflineAdapterError("External transport is disabled")
        now = now or datetime.now(timezone.utc)
        try:
            validate_envelope(
                envelope,
                now=now,
                seen_message_ids=self._seen_message_ids,
                seen_idempotency_keys=self._seen_idempotency_keys,
                expected_sequence=expected_sequence,
            )
        except EnvelopeError as exc:
            self._record_receipt(envelope, "rejected", str(exc), now)
            raise OfflineAdapterError(str(exc)) from exc

        # Record in ledger as evidence only
        self.ledger.record_event(
            event_type=EventType.ASSIGNMENT_CREATED if envelope.payload.get("type") == "assignment" else EventType.IMPLEMENTATION_SUBMITTED,
            project_id=envelope.project_id,
            task_id=envelope.task_id,
            checkpoint_id=envelope.checkpoint_id,
            actor=envelope.sender,
            payload={
                "assignment_id": envelope.assignment_id,
                "synthetic": True,
                "schema_version": envelope.schema_version,
                "digest": envelope.payload_digest,
            },
            message_id=envelope.message_id,
            idempotency_key=envelope.idempotency_key,
            expected_revision=0 if envelope.sequence == 1 else None,
            now=now,
        )
        self._seen_message_ids.add(envelope.message_id)
        self._seen_idempotency_keys.add(envelope.idempotency_key)
        self._record_receipt(envelope, "accepted", None, now)
        return "accepted"
