"""
SKIPPY AI Collaboration Bridge — BRIDGE-02B.2.1 Offline Admission Gate.

OFFLINE SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Durable reservation of message identity. Does not write the protected
BRIDGE-02A ledger. Does not modify the BRIDGE-02B.1 verifier.

A caller-supplied verified flag is never authorization.
execution_enabled remains False. External transport remains disabled.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from skippy.collaboration.offline_signature_verifier import (
    SignatureError,
    SignedEnvelope,
    TrustRoot,
    verify_signature,
)

ALLOWED_TYPES = frozenset({"assignment", "implementation", "correction", "review"})


class AdmissionError(Exception):
    """Fail-closed admission error."""


class EvidenceVerifier(Protocol):
    def verify(
        self,
        kind: str,
        assignment_id: str,
        bound_sha: str,
        evidence_id: str,
        now: datetime,
    ) -> str:
        """Return 'verified', 'rejected', or 'unavailable'."""


class HaltProbe(Protocol):
    def read(self) -> str:
        """Return 'clear', 'halted', or 'unavailable'."""


@dataclass(frozen=True)
class ReservationResult:
    outcome: str  # reserved | duplicate | rejected
    reason: str


class OfflineAdmissionGate:
    def __init__(
        self,
        database_path: Path,
        trust_root: TrustRoot,
        evidence_verifier: EvidenceVerifier,
        halt_probe: HaltProbe,
    ) -> None:
        self.database_path = Path(database_path)
        self.trust_root = trust_root
        self.evidence_verifier = evidence_verifier
        self.halt_probe = halt_probe
        self._init_db()
        self._block_if_unresolved()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _init_db(self) -> None:
        connection = None
        try:
            connection = self._connect()
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    payload_digest BLOB NOT NULL,
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_message
                ON reservations(project_id, task_id, message_id)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_idem
                ON reservations(project_id, task_id, idempotency_key)
                """
            )
            connection.commit()
        except sqlite3.Error as exc:
            raise AdmissionError(f"storage failure during initialization: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def _block_if_unresolved(self) -> None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT state FROM reservations WHERE state IN ('PENDING', 'UNCERTAIN') LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise AdmissionError(f"storage failure: {exc}") from exc
        if row:
            raise AdmissionError(f"Unresolved {row[0]} reservation blocks admission")

    def reserve(self, envelope: SignedEnvelope, now: datetime) -> ReservationResult:
        self._block_if_unresolved()

        try:
            halt = self.halt_probe.read()
        except Exception:
            return ReservationResult("rejected", "HALT probe failed closed")
        if halt != "clear":
            return ReservationResult("rejected", f"HALT state {halt}")

        try:
            verdict = verify_signature(envelope, self.trust_root, now)
        except SignatureError as exc:
            return ReservationResult("rejected", str(exc))
        if verdict != "cryptographically_valid":
            return ReservationResult("rejected", "signature not valid")

        msg_type = envelope.payload.get("type")
        if msg_type not in ALLOWED_TYPES:
            return ReservationResult("rejected", "unauthorized message type")

        evidence_id = str(envelope.payload.get("evidence_id", "")).strip()
        if not evidence_id:
            return ReservationResult("rejected", "missing or empty evidence_id")

        bound_sha = str(envelope.payload.get("implementation_sha", ""))
        evidence_kind = "skippy_review" if msg_type == "review" else "chief_authorization"
        try:
            evidence = self.evidence_verifier.verify(
                evidence_kind,
                envelope.assignment_id,
                bound_sha,
                evidence_id,
                now,
            )
        except Exception:
            return ReservationResult("rejected", "evidence verification failed closed")
        if evidence != "verified":
            return ReservationResult("rejected", f"evidence {evidence}")

        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT project_id, task_id, checkpoint_id, assignment_id,
                       message_id, idempotency_key, sender, payload_digest,
                       sequence, state
                FROM reservations
                WHERE project_id = ? AND task_id = ?
                  AND (message_id = ? OR idempotency_key = ?)
                """,
                (
                    envelope.project_id,
                    envelope.task_id,
                    envelope.message_id,
                    envelope.idempotency_key,
                ),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    connection.rollback()
                    return ReservationResult("rejected", "ambiguous or conflicting identity matches")
                (
                    proj, task, ckpt, assign,
                    mid, idem, sender, digest,
                    seq, state,
                ) = rows[0]
                if (
                    proj == envelope.project_id
                    and task == envelope.task_id
                    and ckpt == envelope.checkpoint_id
                    and assign == envelope.assignment_id
                    and mid == envelope.message_id
                    and idem == envelope.idempotency_key
                    and sender == envelope.sender
                    and digest == envelope.payload_digest
                    and seq == envelope.sequence
                    and state == "RESERVED"
                ):
                    connection.rollback()
                    return ReservationResult("duplicate", "identical reservation")
                connection.rollback()
                return ReservationResult("rejected", "conflicting identity")

            last = connection.execute(
                """
                SELECT sequence, assignment_id FROM reservations
                WHERE project_id = ? AND task_id = ? AND checkpoint_id = ?
                  AND state = 'RESERVED'
                ORDER BY sequence DESC LIMIT 1
                """,
                (envelope.project_id, envelope.task_id, envelope.checkpoint_id),
            ).fetchone()
            expected = 1 if last is None else last[0] + 1
            if envelope.sequence != expected:
                connection.rollback()
                return ReservationResult("rejected", f"sequence {envelope.sequence} != {expected}")
            if last is not None and last[1] != envelope.assignment_id:
                connection.rollback()
                return ReservationResult("rejected", "assignment binding mismatch")

            connection.execute(
                """
                INSERT INTO reservations (
                    project_id, task_id, checkpoint_id, assignment_id,
                    message_id, idempotency_key, sender, payload_digest,
                    sequence, state, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?)
                """,
                (
                    envelope.project_id,
                    envelope.task_id,
                    envelope.checkpoint_id,
                    envelope.assignment_id,
                    envelope.message_id,
                    envelope.idempotency_key,
                    envelope.sender,
                    envelope.payload_digest,
                    envelope.sequence,
                    now.isoformat(),
                ),
            )
            connection.commit()
            return ReservationResult("reserved", "ok")
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise AdmissionError(f"storage failure: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
