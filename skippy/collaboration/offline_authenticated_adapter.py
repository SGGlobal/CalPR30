"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A.1 Offline Adapter Stub.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

External transport is disabled. This adapter accepts only synthetic
fixtures and records immutable audit receipts. It does not send,
retry, or enable execution.

SECURITY NOTES:
- payload_digest is an integrity checksum, not actor authentication.
- Replay protection is durable (persisted) and checked under a lock.
- Ledger revision is derived from the ledger, never from message sequence.
- Audit receipts are append-only. Partial writes are reported as UNCERTAIN.
- No cross-database atomicity is claimed.
execution_enabled remains False.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from skippy.collaboration.authenticated_envelope import (
    AuthenticatedEnvelope,
    EnvelopeError,
    validate_envelope,
)
from skippy.collaboration.checkpoint_ledger import CheckpointLedger, EventType, LedgerError


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
        self._lock = threading.Lock()
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
            # Append-only: no UPDATE/DELETE triggers that allow mutation
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_receipts_no_update
                BEFORE UPDATE ON audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'audit_receipts is append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_receipts_no_delete
                BEFORE DELETE ON audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'audit_receipts is append-only');
                END
                """
            )

    def _seen_ids(self) -> tuple[set[str], set[str]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT message_id, idempotency_key FROM audit_receipts WHERE outcome = 'accepted'"
            ).fetchall()
        return {r[0] for r in rows}, {r[1] for r in rows}

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
        halted: bool = False,
    ) -> str:
        """
        Validate and record an envelope. Returns 'accepted' or raises.

        Fail-closed. Does not dispatch external transport. Does not retry.
        If halted is True, the envelope is rejected and a receipt is recorded.
        """
        if self.TRANSPORT_ENABLED:
            raise OfflineAdapterError("External transport is disabled")
        now = now or datetime.now(timezone.utc)

        with self._lock:
            if halted:
                self._record_receipt(envelope, "rejected", "HALTED", now)
                raise OfflineAdapterError("HALTED: envelope rejected")

            seen_msg, seen_idem = self._seen_ids()
            try:
                event_type_name = validate_envelope(
                    envelope,
                    now=now,
                    seen_message_ids=seen_msg,
                    seen_idempotency_keys=seen_idem,
                    expected_sequence=expected_sequence,
                )
            except EnvelopeError as exc:
                self._record_receipt(envelope, "rejected", str(exc), now)
                raise OfflineAdapterError(str(exc)) from exc

            # Derive actual ledger revision; never confuse with message sequence
            with self.ledger._connect() as connection:
                current_rev = self.ledger._current_revision(connection)

            try:
                self.ledger.record_event(
                    event_type=EventType[event_type_name],
                    project_id=envelope.project_id,
                    task_id=envelope.task_id,
                    checkpoint_id=envelope.checkpoint_id,
                    actor=envelope.sender,
                    payload={
                        "assignment_id": envelope.assignment_id,
                        "synthetic": True,
                        "schema_version": envelope.schema_version,
                        "digest": envelope.payload_digest,
                        "type": envelope.payload.get("type"),
                    },
                    message_id=envelope.message_id,
                    idempotency_key=envelope.idempotency_key,
                    expected_revision=current_rev,
                    now=now,
                )
            except LedgerError as exc:
                # Partial persistence or ledger rejection — record UNCERTAIN receipt
                self._record_receipt(envelope, "uncertain", str(exc), now)
                raise OfflineAdapterError(f"Ledger rejection: {exc}") from exc

            self._record_receipt(envelope, "accepted", None, now)
            return "accepted"
