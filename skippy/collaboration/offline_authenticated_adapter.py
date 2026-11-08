"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A.2 Offline Adapter Stub.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

This is an offline integrity contract, not authenticated actor messaging.
External transport is disabled. This adapter accepts only synthetic
fixtures supplied through a controlled trusted_fixture dependency.

SECURITY NOTES:
- payload_digest is an integrity checksum, not actor authentication.
- trusted_fixture is injected by the adapter, not claimed by the envelope.
- Sequence is enforced durably per stream, separate from ledger revision.
- Review and implementation SHA evidence is preserved and bound.
- Partial writes are reported as UNCERTAIN. No cross-database atomicity.
- HALT is derived from authoritative state, not a caller flag.
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
    Disabled-transport adapter for synthetic integrity envelopes.

    Records immutable audit receipts. Fail-closed on any validation
    failure. No external network. No automatic retry. No catch-up after HALT.
    """

    execution_enabled = False
    TRANSPORT_ENABLED = False

    def __init__(
        self,
        ledger: CheckpointLedger,
        database_path: Path,
        *,
        trusted_fixture: bool = False,
        halt_check=None,
    ):
        """
        trusted_fixture: controlled dependency. True only when the caller
        is a test fixture that has established authorization outside the
        envelope. Never treat an inbound claim as Chief approval.
        halt_check: optional callable returning True if HALTED/UNCERTAIN.
        """
        self.ledger = ledger
        self.database_path = Path(database_path)
        self._trusted_fixture = trusted_fixture
        self._halt_check = halt_check or (lambda: False)
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sequence_state (
                    stream_key TEXT PRIMARY KEY,
                    next_sequence INTEGER NOT NULL
                )
                """
            )
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

    def _expected_sequence(self, stream_key: str) -> int:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT next_sequence FROM sequence_state WHERE stream_key = ?",
                (stream_key,),
            ).fetchone()
        return row[0] if row else 1

    def _advance_sequence(self, stream_key: str, next_seq: int) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO sequence_state (stream_key, next_sequence)
                VALUES (?, ?)
                ON CONFLICT(stream_key) DO UPDATE SET next_sequence = excluded.next_sequence
                """,
                (stream_key, next_seq),
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
    ) -> str:
        """
        Validate and record an envelope. Returns 'accepted' or raises.

        Fail-closed. Does not dispatch external transport. Does not retry.
        HALT is derived from the authoritative halt_check, not a caller flag.
        """
        if self.TRANSPORT_ENABLED:
            raise OfflineAdapterError("External transport is disabled")
        now = now or datetime.now(timezone.utc)

        with self._lock:
            # Authoritative HALT check
            try:
                if self._halt_check():
                    self._record_receipt(envelope, "rejected", "HALTED", now)
                    raise OfflineAdapterError("HALTED: envelope rejected")
            except OfflineAdapterError:
                raise
            except Exception as exc:
                self._record_receipt(envelope, "uncertain", f"HALT check failed: {exc}", now)
                raise OfflineAdapterError(f"HALT check failed: {exc}") from exc

            # Assignment requires trusted fixture dependency
            payload_type = envelope.payload.get("type")
            if payload_type == "assignment" and not self._trusted_fixture:
                self._record_receipt(envelope, "rejected", "Assignment requires trusted fixture", now)
                raise OfflineAdapterError("Assignment requires trusted fixture dependency")

            stream_key = f"{envelope.project_id}:{envelope.task_id}"
            expected_seq = self._expected_sequence(stream_key)
            seen_msg, seen_idem = self._seen_ids()

            try:
                event_type_name = validate_envelope(
                    envelope,
                    now=now,
                    seen_message_ids=seen_msg,
                    seen_idempotency_keys=seen_idem,
                    expected_sequence=expected_seq,
                )
            except EnvelopeError as exc:
                self._record_receipt(envelope, "rejected", str(exc), now)
                raise OfflineAdapterError(str(exc)) from exc

            # Build ledger payload preserving SHA evidence
            ledger_payload = {
                "assignment_id": envelope.assignment_id,
                "synthetic": True,
                "schema_version": envelope.schema_version,
                "digest": envelope.payload_digest,
                "type": payload_type,
            }
            if "sha" in envelope.payload:
                ledger_payload["sha"] = envelope.payload["sha"]
            if "source_verified" in envelope.payload:
                ledger_payload["source_verified"] = envelope.payload["source_verified"]
            if "findings" in envelope.payload:
                ledger_payload["findings"] = envelope.payload["findings"]

            # Derive actual ledger revision
            with self.ledger._connect() as connection:
                current_rev = self.ledger._current_revision(connection)

            try:
                self.ledger.record_event(
                    event_type=EventType[event_type_name],
                    project_id=envelope.project_id,
                    task_id=envelope.task_id,
                    checkpoint_id=envelope.checkpoint_id,
                    actor=envelope.sender,
                    payload=ledger_payload,
                    message_id=envelope.message_id,
                    idempotency_key=envelope.idempotency_key,
                    expected_revision=current_rev,
                    now=now,
                )
            except LedgerError as exc:
                self._record_receipt(envelope, "uncertain", str(exc), now)
                raise OfflineAdapterError(f"Ledger rejection: {exc}") from exc

            try:
                self._record_receipt(envelope, "accepted", None, now)
                self._advance_sequence(stream_key, expected_seq + 1)
            except Exception as exc:
                # Ledger succeeded but receipt/sequence failed — UNCERTAIN
                raise OfflineAdapterError(f"Partial write: ledger accepted but receipt failed: {exc}") from exc

            return "accepted"
