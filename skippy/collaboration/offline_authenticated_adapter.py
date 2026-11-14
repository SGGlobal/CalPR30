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
- Reconciliation collects the receipts view and the ledger view while one
  receipts BEGIN IMMEDIATE lock is held (coordinated reads under the
  W1 PENDING -> W2 ledger -> W3 ACCEPTED protocol, not cross-database
  atomic commits). One receipts database serves the adapter-origin events
  in a ledger; two receipts databases writing adapter-origin events into
  one ledger are outside this guarantee.
- HALT is derived from authoritative state, not a caller flag.
execution_enabled remains False.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skippy.collaboration.authenticated_envelope import (
    AuthenticatedEnvelope,
    EnvelopeError,
    validate_envelope,
)
from skippy.collaboration.checkpoint_ledger import (
    ALLOWED_TRANSITIONS,
    CheckpointLedger,
    CheckpointState,
    EventType,
    LedgerError,
)
from skippy.collaboration.offline_admission_gate import (
    AUTHORIZATION_MATRIX,
    OfflineAdmissionGate,
    ReservationResult,
    _evidence_id_problem,
    _sha_problem,
    _snapshot,
)
from skippy.collaboration.offline_signature_verifier import (
    SignedEnvelope,
    _canonical_json,
    canonical_bytes,
    verify_signature,
)


class OfflineAdapterError(RuntimeError):
    """Raised when the offline adapter cannot process an envelope safely."""


class SignedAdmissionError(OfflineAdapterError):
    """J6 signed-path failure carrying the caller's own J2 classification.

    classification: 'refused' (the failing step issued no write),
    'rejected' (a definite denial whose single commit was confirmed),
    'stop_retained' (confirmed S9 HALT record; G kept),
    'confirmed_rolled_back' (condition (a)) or 'ambiguous' (all other
    outcomes, including RF). Observer knowledge never upgrades it.
    """

    def __init__(self, message: str, *, step: str, classification: str, poisoned: bool = False):
        super().__init__(message)
        self.step = step
        self.classification = classification
        self.poisoned = poisoned


class _SignedRefusal(Exception):
    """Internal: a read-only refusal raised before any write of the step."""


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
        halt_check: required callable returning True if HALTED/UNCERTAIN.
        Omitted or falsey halt_check is rejected.
        """
        if halt_check is None or not callable(halt_check):
            raise OfflineAdapterError("Authoritative HALT check is required")
        self.ledger = ledger
        self.database_path = Path(database_path)
        self._trusted_fixture = trusted_fixture
        self._halt_check = halt_check
        self._lock = threading.Lock()
        self._init_receipts()
        self._schema_preflight()
        # Deterministic reconciliation on startup
        self._reconcile()

    def _after_pending_commit(self) -> None:
        """Test-only seam. No-op in production. Tests may monkeypatch to pause."""
        return

    def _schema_preflight(self) -> None:
        """
        Fail closed if an existing intent_journal lacks UNIQUE constraints.

        CREATE TABLE IF NOT EXISTS does not migrate an old table. Inspect
        actual indices and fail closed if uniqueness is missing.
        """
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='intent_journal'"
            ).fetchall()
            if not tables:
                return  # fresh database; CREATE TABLE will apply constraints
            indices = connection.execute("PRAGMA index_list(intent_journal)").fetchall()
            single_column_unique = set()
            for idx in indices:
                unique = idx[2]
                partial = idx[4] if len(idx) > 4 else 0
                if unique and not partial:  # unique and not partial
                    info = connection.execute(f"PRAGMA index_info({idx[1]})").fetchall()
                    # Only non-partial single-column unique indices count
                    if len(info) == 1:
                        single_column_unique.add(info[0][2])
            if "message_id" not in single_column_unique or "idempotency_key" not in single_column_unique:
                raise OfflineAdapterError(
                    "Legacy intent_journal lacks independent non-partial single-column UNIQUE constraints on message_id and idempotency_key; migration required"
                )

    def _read_adapter_ledger_view(self) -> dict:
        """
        Collect adapter-origin ledger events for reconciliation.

        Opens and closes its own ledger connection; never touches the
        receipts connection. Called only while the receipts BEGIN IMMEDIATE
        lock is held, after the receipts view has been collected.
        """
        try:
            with closing(self.ledger._connect()) as ledger_conn, ledger_conn:
                ledger_events = ledger_conn.execute(
                    "SELECT message_id, idempotency_key, project_id, task_id, checkpoint_id, actor, payload FROM events ORDER BY revision"
                ).fetchall()
            adapter_ledger = {}
            for row in ledger_events:
                msg_id, idem, proj, task, cp, actor, payload_json = row
                try:
                    payload = json.loads(payload_json) if payload_json else {}
                except Exception as exc:
                    raise OfflineAdapterError(
                        f"Malformed ledger payload for {msg_id}: {exc}"
                    ) from exc
                if payload.get("adapter_origin") == "bridge-02a":
                    adapter_ledger[msg_id] = {
                        "idempotency_key": idem,
                        "project_id": proj,
                        "task_id": task,
                        "checkpoint_id": cp,
                        "actor": actor,
                        "assignment_id": payload.get("assignment_id"),
                        "digest": payload.get("digest"),
                    }
        except Exception as exc:
            raise OfflineAdapterError(f"Ledger unreadable during reconciliation: {exc}") from exc
        return adapter_ledger

    def _collect_reconcile_views(self, connection):
        """
        Collect the receipts view, then the ledger view.

        ``connection`` is a receipts connection already inside BEGIN
        IMMEDIATE. The four receipts queries run in that one transaction and
        the ledger view is read while that lock is still held. This method
        never begins, commits, rolls back or closes ``connection``.

        Returns None only for the legacy fresh-store case (journal file
        absent and no adapter ledger events).
        """
        try:
            unresolved = connection.execute(
                "SELECT message_id, state FROM intent_journal WHERE state IN ('PENDING', 'UNCERTAIN')"
            ).fetchall()
            accepted = connection.execute(
                """
                SELECT message_id, idempotency_key, project_id, task_id,
                       checkpoint_id, assignment_id, payload_digest, sequence
                FROM intent_journal WHERE state = 'ACCEPTED'
                """
            ).fetchall()
            receipts = connection.execute(
                """
                SELECT message_id, idempotency_key, project_id, task_id,
                       checkpoint_id, assignment_id, sender
                FROM audit_receipts WHERE outcome = 'accepted'
                """
            ).fetchall()
            seq_rows = connection.execute(
                "SELECT stream_key, next_sequence FROM sequence_state"
            ).fetchall()
            unresolved_ids = {r[0] for r in unresolved}
            accepted_rows = {r[0]: r for r in accepted}
            receipt_rows = {r[0]: r for r in receipts}
            receipt_ids = set(receipt_rows.keys())
            # Detect duplicate accepted receipts (same message_id appears more than once)
            receipt_counts: dict[str, int] = {}
            for r in receipts:
                receipt_counts[r[0]] = receipt_counts.get(r[0], 0) + 1
            duplicates = [mid for mid, count in receipt_counts.items() if count > 1]
            if duplicates:
                raise OfflineAdapterError(f"Duplicate accepted receipts: {duplicates}")
            seq_map = {r[0]: r[1] for r in seq_rows}
        except Exception as exc:
            # Fail closed on unreadable journal if the database file exists
            # (distinguishes corrupt store from fresh empty setup)
            if self.database_path.exists():
                raise OfflineAdapterError(
                    f"Intent journal unreadable: {exc}"
                ) from exc
            if self._read_adapter_ledger_view():
                raise OfflineAdapterError(
                    f"Intent journal unreadable and adapter ledger events exist: {exc}"
                ) from exc
            return None

        # Ledger view is collected while the receipts lock is still held.
        adapter_ledger = self._read_adapter_ledger_view()
        return adapter_ledger, unresolved_ids, accepted_rows, receipt_rows, receipt_ids, seq_map

    def _reconcile(self, connection: sqlite3.Connection | None = None) -> None:
        """
        Scoped reconciliation of adapter-originated events.

        Compares only ledger events that carry the adapter origin marker
        against the intent journal. Independent BRIDGE-01 events are ignored.
        Any PENDING intent is ambiguous and fails closed. Any ACCEPTED intent
        without a matching ledger event fails closed. Never auto-recovers.

        Locking protocol: the four receipts queries run inside one receipts
        BEGIN IMMEDIATE transaction, the ledger view is read while that lock
        is still held, and the lock is released only after both views are
        collected. Adapters write a ledger event only after committing its
        PENDING intent (W1 -> W2 -> W3), so an in-flight admission is seen
        as PENDING (Unresolved) and cannot produce a torn cross-database
        view. These are coordinated reads, not a cross-database atomic
        commit.

        connection is None (constructor path): this call owns its receipts
        connection: open, BEGIN IMMEDIATE, collect both views, ROLLBACK (it
        writes nothing), close. On failure it rolls back only if a
        transaction exists and always closes before propagating.

        connection given (accept path): borrowed. The caller has executed
        BEGIN IMMEDIATE and is the sole owner of commit, rollback and close.
        This call never begins, commits, rolls back or closes it, on success
        or failure.
        """
        if connection is not None:
            if not connection.in_transaction:
                raise OfflineAdapterError(
                    "Reconciliation requires the caller's open receipts transaction"
                )
            views = self._collect_reconcile_views(connection)
        else:
            try:
                owned = sqlite3.connect(self.database_path)
            except Exception as exc:
                raise OfflineAdapterError(f"Intent journal unreadable: {exc}") from exc
            try:
                try:
                    owned.execute("BEGIN IMMEDIATE")
                except Exception as exc:
                    raise OfflineAdapterError(f"Intent journal unreadable: {exc}") from exc
                views = self._collect_reconcile_views(owned)
            finally:
                try:
                    if owned.in_transaction:
                        owned.rollback()
                finally:
                    owned.close()
        if views is None:
            return
        adapter_ledger, unresolved_ids, accepted_rows, receipt_rows, receipt_ids, seq_map = views

        # PENDING or UNCERTAIN is always unresolved — fail closed
        if unresolved_ids:
            raise OfflineAdapterError(
                f"Unresolved intent requires manual reconciliation: {sorted(unresolved_ids)}"
            )

        # Bidirectional exact identity comparison
        for msg_id, journal_row in accepted_rows.items():
            if msg_id not in adapter_ledger:
                raise OfflineAdapterError(f"ACCEPTED intent without matching ledger event: {msg_id}")
            ledger_row = adapter_ledger[msg_id]
            if (
                journal_row[1] != ledger_row["idempotency_key"]
                or journal_row[2] != ledger_row["project_id"]
                or journal_row[3] != ledger_row["task_id"]
                or journal_row[4] != ledger_row["checkpoint_id"]
                or journal_row[5] != ledger_row["assignment_id"]
                or journal_row[6] != ledger_row["digest"]
            ):
                raise OfflineAdapterError(f"Identity mismatch for {msg_id}")
            if msg_id not in receipt_ids:
                raise OfflineAdapterError(f"Missing accepted receipt for {msg_id}")
            receipt = receipt_rows[msg_id]
            if (
                receipt[1] != journal_row[1]
                or receipt[2] != journal_row[2]
                or receipt[3] != journal_row[3]
                or receipt[4] != journal_row[4]
                or receipt[5] != journal_row[5]
            ):
                raise OfflineAdapterError(f"Receipt identity mismatch for {msg_id}")
            # Sender must match ledger actor
            if receipt[6] != ledger_row["actor"]:
                raise OfflineAdapterError(f"Sender mismatch for {msg_id}")

        # Reverse: accepted receipts without matching journal intents
        for msg_id in receipt_ids:
            if msg_id not in accepted_rows:
                raise OfflineAdapterError(f"Extra accepted receipt without journal intent: {msg_id}")

        for msg_id in adapter_ledger:
            if msg_id not in accepted_rows:
                raise OfflineAdapterError(f"Adapter ledger event without ACCEPTED intent: {msg_id}")

        # Sequence reconstruction per stream (deterministic, ordered)
        from collections import defaultdict
        streams: dict[str, list[int]] = defaultdict(list)
        for row in accepted_rows.values():
            stream = f"{row[2]}:{row[3]}"
            streams[stream].append(row[7])
        for stream, seqs in streams.items():
            seqs.sort()
            if seqs[0] != 1:
                raise OfflineAdapterError(f"Sequence does not start at 1 for {stream}")
            for i, s in enumerate(seqs):
                if s != i + 1:
                    raise OfflineAdapterError(f"Sequence gap in stream {stream}")
            expected = seq_map.get(stream)
            if expected is None or expected != seqs[-1] + 1:
                raise OfflineAdapterError(f"Sequence state mismatch or missing for {stream}")
        # Orphan sequence state
        for stream in seq_map:
            if stream not in streams:
                raise OfflineAdapterError(f"Orphan sequence state for {stream}")

    def _init_receipts(self):
        """P-9 legacy constructor guard (work order 6870819489, amended 6871180075).

        One receipts ``BEGIN IMMEDIATE`` is acquired BEFORE any DDL or INSERT.
        Under that same transaction ``sqlite_master`` is read for the reserved
        schema-object name ``signed_admission_marker`` (any object kind, any
        letter case, regardless of marker rows or value). If it is present
        the constructor refuses with ``OfflineAdapterError``: no DDL, no
        INSERT, and neither ``_schema_preflight`` nor ``_reconcile`` runs. The
        stop row and any stored reason (G) are never read, parsed or adopted.

        If the name is absent, the unchanged legacy statements run in that
        same transaction and commit. Successful unmarked end states are
        unchanged (a missing file is still created unmarked); failure
        atomicity intentionally changes: a failure before commit no longer
        leaves autocommitted partial DDL behind.

        Open, lock, read and initialization errors become
        ``OfflineAdapterError`` with the cause chained. The connection is
        closed on every outcome, and a rollback that raised is never reported
        as confirmed. No retry, no repair of a marked store, no marker
        reconstruction and no compensating write. A store whose marker object
        was removed out of band is indistinguishable from an unmarked store
        (stated limit, not a safety claim).
        """
        try:
            connection = _legacy_init_connect(self.database_path)
        except Exception as exc:
            raise OfflineAdapterError(
                f"Legacy constructor refused: receipts store not openable ({type(exc).__name__}: {exc}); "
                "no initialization write issued"
            ) from exc
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                raise OfflineAdapterError(
                    "Legacy constructor refused: receipts lock not acquired or store unreadable "
                    f"({type(exc).__name__}: {exc}); no initialization write issued"
                ) from exc
            try:
                reserved = connection.execute(
                    "SELECT type FROM sqlite_master WHERE lower(name) = ?",
                    (_SIGNED_MARKER_OBJECT,),
                ).fetchall()
            except Exception as exc:
                self._legacy_init_abort(
                    connection,
                    "Legacy constructor refused: receipts store unreadable; no initialization write issued",
                    exc,
                )
            if reserved:
                kinds = ", ".join(sorted({str(row[0]) for row in reserved}))
                rollback = (
                    "rollback returned" if self._legacy_init_rollback(connection)
                    else "rollback raised; lock release outcome unknown"
                )
                raise OfflineAdapterError(
                    "Legacy constructor refused: receipts store carries reserved schema object "
                    f"signed_admission_marker ({kinds}); no initialization write issued; {rollback}"
                )
            try:
                for statement in _LEGACY_INIT_STATEMENTS:
                    connection.execute(statement)
            except Exception as exc:
                self._legacy_init_abort(connection, "Legacy initialization failed before commit", exc)
            try:
                connection.commit()
            except Exception as exc:
                raise OfflineAdapterError(
                    f"Legacy initialization commit outcome unknown ({type(exc).__name__}: {exc}); "
                    "inspect manually; no retry"
                ) from exc
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - cleanup only; outcome already decided
                pass

    @staticmethod
    def _legacy_init_rollback(connection) -> bool:
        """True only when rollback() returned normally (never assumed)."""
        try:
            connection.rollback()
            return True
        except Exception:  # noqa: BLE001 - reported to the caller as unconfirmed
            return False

    def _legacy_init_abort(self, connection, message, exc):
        """Failure before commit() was invoked: report the rollback as observed."""
        if self._legacy_init_rollback(connection):
            raise OfflineAdapterError(
                f"{message} ({type(exc).__name__}: {exc}); rollback returned; nothing committed by this call"
            ) from exc
        raise OfflineAdapterError(
            f"{message} ({type(exc).__name__}: {exc}); rollback raised; outcome unknown; inspect manually"
        ) from exc

    def _seen_ids(self) -> tuple[set[str], set[str]]:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            rows = connection.execute(
                "SELECT message_id, idempotency_key FROM audit_receipts WHERE outcome = 'accepted'"
            ).fetchall()
        return {r[0] for r in rows}, {r[1] for r in rows}

    def _expected_sequence(self, stream_key: str) -> int:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            row = connection.execute(
                "SELECT next_sequence FROM sequence_state WHERE stream_key = ?",
                (stream_key,),
            ).fetchone()
        return row[0] if row else 1

    def _advance_sequence(self, stream_key: str, next_seq: int) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO sequence_state (stream_key, next_sequence)
                VALUES (?, ?)
                ON CONFLICT(stream_key) DO UPDATE SET next_sequence = excluded.next_sequence
                """,
                (stream_key, next_seq),
            )

    def _record_receipt_in(
        self,
        connection,
        envelope: AuthenticatedEnvelope,
        outcome: str,
        reason: str | None,
        now: datetime,
    ) -> None:
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

    def _record_receipt(
        self,
        envelope: AuthenticatedEnvelope,
        outcome: str,
        reason: str | None,
        now: datetime,
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
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
        if getattr(self, "_signed_mode", False):
            # P-2 (work order 6868633069): a signed-admission instance never
            # runs the legacy unsigned path.
            raise OfflineAdapterError("legacy unsigned path disabled on a signed-admission instance")
        now = now or datetime.now(timezone.utc)

        with self._lock:
            # Persistent fail-stop gate and exclusive admission. accept() is the
            # sole owner of this receipts connection: commit, rollback, close.
            with closing(sqlite3.connect(self.database_path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")

                # Reconciliation gate before any admission, inside this same
                # receipts transaction (borrowed; never committed, rolled back
                # or closed by _reconcile).
                self._reconcile(connection)

                row = connection.execute(
                    "SELECT stopped, reason FROM fail_stop WHERE singleton = 1"
                ).fetchone()
                if row and row[0]:
                    raise OfflineAdapterError(f"Fail-stop active: {row[1]}")

                # P-2 (work order 6868633069): legacy accept() refuses any
                # receipts store provisioned for signed admission, whether or
                # not its stop is currently armed. Unmarked stores: unchanged.
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'signed_admission_marker'"
                ).fetchone():
                    raise OfflineAdapterError(
                        "Legacy accept() refused: receipts store carries signed_admission_marker"
                    )

                # Admission-time durable single-active-intent gate
                active = connection.execute(
                    "SELECT message_id FROM intent_journal WHERE state IN ('PENDING', 'UNCERTAIN')"
                ).fetchall()
                if active:
                    raise OfflineAdapterError(
                        f"Active unresolved intent blocks admission: {[r[0] for r in active]}"
                    )

                # Authoritative HALT check
                try:
                    if self._halt_check():
                        self._record_receipt_in(connection, envelope, "rejected", "HALTED", now)
                        connection.commit()
                        raise OfflineAdapterError("HALTED: envelope rejected")
                except OfflineAdapterError:
                    raise
                except Exception as exc:
                    self._record_receipt_in(connection, envelope, "uncertain", f"HALT check failed: {exc}", now)
                    connection.commit()
                    raise OfflineAdapterError(f"HALT check failed: {exc}") from exc

                # Assignment requires trusted fixture dependency
                try:
                    payload_type = envelope.payload.get("type")
                except EnvelopeError as exc:
                    self._record_receipt_in(connection, envelope, "rejected", str(exc), now)
                    connection.commit()
                    raise OfflineAdapterError(str(exc)) from exc

                if payload_type == "assignment" and not self._trusted_fixture:
                    self._record_receipt_in(connection, envelope, "rejected", "Assignment requires trusted fixture", now)
                    connection.commit()
                    raise OfflineAdapterError("Assignment requires trusted fixture dependency")

                # Assignment identity consistency
                prior = connection.execute(
                    "SELECT assignment_id FROM assignment_identity WHERE project_id = ? AND task_id = ?",
                    (envelope.project_id, envelope.task_id),
                ).fetchone()
                if prior and prior[0] != envelope.assignment_id:
                    self._record_receipt_in(connection, envelope, "rejected", "Assignment identity mismatch", now)
                    connection.commit()
                    raise OfflineAdapterError("Assignment identity mismatch")

                stream_key = f"{envelope.project_id}:{envelope.task_id}"
                seq_row = connection.execute(
                    "SELECT next_sequence FROM sequence_state WHERE stream_key = ?",
                    (stream_key,),
                ).fetchone()
                expected_seq = seq_row[0] if seq_row else 1

                seen_rows = connection.execute(
                    "SELECT message_id, idempotency_key FROM audit_receipts WHERE outcome = 'accepted'"
                ).fetchall()
                seen_msg = {r[0] for r in seen_rows}
                seen_idem = {r[1] for r in seen_rows}

                try:
                    event_type_name = validate_envelope(
                        envelope,
                        now=now,
                        seen_message_ids=seen_msg,
                        seen_idempotency_keys=seen_idem,
                        expected_sequence=expected_seq,
                    )
                except EnvelopeError as exc:
                    self._record_receipt_in(connection, envelope, "rejected", str(exc), now)
                    connection.commit()
                    raise OfflineAdapterError(str(exc)) from exc

                # Build ledger payload preserving SHA evidence and adapter origin
                ledger_payload = {
                    "assignment_id": envelope.assignment_id,
                    "synthetic": True,
                    "schema_version": envelope.schema_version,
                    "digest": envelope.payload_digest,
                    "type": payload_type,
                    "adapter_origin": "bridge-02a",
                }
                if "sha" in envelope.payload:
                    ledger_payload["sha"] = envelope.payload["sha"]
                if "source_verified" in envelope.payload:
                    ledger_payload["source_verified"] = envelope.payload["source_verified"]
                if "findings" in envelope.payload:
                    ledger_payload["findings"] = envelope.payload["findings"]

                # Record durable PENDING intent before ledger write
                connection.execute(
                    """
                    INSERT INTO intent_journal (
                        message_id, idempotency_key, project_id, task_id,
                        checkpoint_id, assignment_id, sequence, payload_digest,
                        state, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                    """,
                    (
                        envelope.message_id,
                        envelope.idempotency_key,
                        envelope.project_id,
                        envelope.task_id,
                        envelope.checkpoint_id,
                        envelope.assignment_id,
                        expected_seq,
                        envelope.payload_digest,
                        now.isoformat(),
                    ),
                )
                connection.commit()

                # Test-only pause hook after PENDING commit and before ledger write.
                # No-op in production. Tests may monkeypatch _after_pending_commit.
                self._after_pending_commit()

                # Derive actual ledger revision
                with closing(self.ledger._connect()) as ledger_conn, ledger_conn:
                    current_rev = self.ledger._current_revision(ledger_conn)

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
                    connection.execute(
                        "UPDATE intent_journal SET state = 'UNCERTAIN' WHERE message_id = ? AND state = 'PENDING'",
                        (envelope.message_id,),
                    )
                    connection.commit()
                    raise OfflineAdapterError(f"Ledger rejection: {exc}") from exc

                # Mark ACCEPTED only after successful ledger commit
                cursor = connection.execute(
                    "UPDATE intent_journal SET state = 'ACCEPTED' WHERE message_id = ? AND state = 'PENDING'",
                    (envelope.message_id,),
                )
                if cursor.rowcount != 1:
                    raise OfflineAdapterError("Intent state transition failed")

                # Atomic receipt + sequence + assignment identity
                try:
                    self._record_receipt_in(connection, envelope, "accepted", None, now)
                    connection.execute(
                        """
                        INSERT INTO sequence_state (stream_key, next_sequence)
                        VALUES (?, ?)
                        ON CONFLICT(stream_key) DO UPDATE SET next_sequence = excluded.next_sequence
                        WHERE next_sequence = ?
                        """,
                        (stream_key, expected_seq + 1, expected_seq),
                    )
                    if payload_type == "assignment":
                        connection.execute(
                            """
                            INSERT INTO assignment_identity
                            (project_id, task_id, assignment_id) VALUES (?, ?, ?)
                            """,
                            (envelope.project_id, envelope.task_id, envelope.assignment_id),
                        )
                    connection.commit()
                except Exception as exc:
                    # Rollback the failed transaction first, then record fail-stop
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                    try:
                        connection.execute(
                            "UPDATE fail_stop SET stopped = 1, reason = ? WHERE singleton = 1",
                            (f"Partial write: {exc}",),
                        )
                        connection.commit()
                    except Exception:
                        pass  # fail-stop itself failed; next admission will reconcile
                    raise OfflineAdapterError(f"Partial write: {exc}") from exc

            return "accepted"

    # ------------------------------------------------------------------
    # BRIDGE-02B.2.2 J6 offline disposable-store prototype (work order
    # 6868633069). OFFLINE SYNTHETIC ONLY. Governing text: J1-J8 of
    # docs/bridge-02b2-compatibility-contract.md at 4d05c321 (C1-C6, Rule U,
    # J3 rows 1-9). execution_enabled stays False and transport stays
    # disabled. Cooperative, adapter-mediated admission on ONE bound
    # receipts store only; direct gate and ledger callers, other receipts
    # databases and file-level writers are outside every claim (J4). The
    # provisioning marker proves provisioning only, never store identity or
    # freshness, and the nonce inside G authenticates nothing. There is no
    # recovery API.
    # ------------------------------------------------------------------

    @classmethod
    def open_signed(
        cls,
        ledger: CheckpointLedger,
        database_path: Path,
        *,
        admission_gate: OfflineAdmissionGate,
        halt_check,
        trusted_fixture: bool = False,
        busy_timeout: float = 5.0,
    ) -> "OfflineAuthenticatedAdapter":
        """P-4 gated signed construction (J3 row 1).

        Opens only an already-provisioned receipts store. Never runs the
        legacy auto-creating ``_init_receipts`` path, never creates a file and
        writes nothing. Refuses unless every C1 phase 1 predicate holds in a
        read-only receipts transaction: D1-D3 through ``_receipts_connect``,
        marker, exactly one ``fail_stop`` row with ``stopped = 0``, no
        PENDING/UNCERTAIN intent, reconcile, signed-record shape and Rule U.
        A stored stop reason is never read, parsed or adopted (C6).
        """
        if cls.TRANSPORT_ENABLED or cls.execution_enabled:
            raise SignedAdmissionError(
                "signed admission requires transport and execution disabled",
                step="construct", classification="refused",
            )
        if halt_check is None or not callable(halt_check):
            raise SignedAdmissionError(
                "Authoritative HALT check is required", step="construct", classification="refused"
            )
        if type(ledger) is not CheckpointLedger:
            raise SignedAdmissionError(
                "signed admission requires a CheckpointLedger", step="construct", classification="refused"
            )
        if type(admission_gate) is not OfflineAdmissionGate:
            raise SignedAdmissionError(
                "signed admission requires an OfflineAdmissionGate", step="construct",
                classification="refused",
            )
        if type(trusted_fixture) is not bool:
            raise SignedAdmissionError(
                "trusted_fixture must be a bool", step="construct", classification="refused"
            )
        if (
            type(busy_timeout) not in (int, float)
            or not 0 < busy_timeout <= SIGNED_MAX_BUSY_TIMEOUT
        ):
            raise SignedAdmissionError(
                "busy_timeout must be a bounded positive number of seconds", step="construct",
                classification="refused",
            )
        receipts_path = _signed_store_path(database_path, "construct")
        try:
            resolved = {
                os.path.normcase(str(receipts_path.resolve())),
                os.path.normcase(str(Path(ledger.database_path).resolve())),
                os.path.normcase(str(Path(admission_gate.database_path).resolve())),
            }
        except (OSError, RuntimeError) as exc:
            raise SignedAdmissionError(
                f"store paths unresolvable: {exc}", step="construct", classification="refused"
            ) from exc
        if len(resolved) != 3:
            raise SignedAdmissionError(
                "receipts, ledger and admission stores must be pairwise distinct", step="construct",
                classification="refused",
            )
        self = cls.__new__(cls)
        self.ledger = ledger
        self.database_path = receipts_path
        self._trusted_fixture = trusted_fixture
        self._halt_check = halt_check
        self._lock = threading.Lock()
        self._signed_mode = True
        self._signed_gate = admission_gate
        self._signed_busy_timeout = float(busy_timeout)
        self._signed_poison = None
        self._signed_last_config = None
        self._signed_construction_check()
        return self

    def _signed_construction_check(self) -> None:
        try:
            connection = self._receipts_connect()
        except Exception as exc:
            raise SignedAdmissionError(
                f"signed construction refused: {exc}", step="construct", classification="refused"
            ) from exc
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                raise SignedAdmissionError(
                    f"signed construction refused: {exc}", step="construct", classification="refused"
                ) from exc
            try:
                self._signed_phase1(connection)
            except _SignedRefusal as refusal:
                if not self._signed_rollback(connection):
                    raise SignedAdmissionError(
                        f"signed construction: rollback unconfirmed after refusal ({refusal})",
                        step="construct", classification="ambiguous",
                    ) from None
                raise SignedAdmissionError(
                    f"signed construction refused: {refusal}", step="construct", classification="refused"
                ) from None
            if not self._signed_rollback(connection):
                raise SignedAdmissionError(
                    "signed construction: rollback unconfirmed", step="construct",
                    classification="ambiguous",
                )
        finally:
            self._signed_close(connection)

    def _receipts_connect(self):
        """P-6 private receipts connection factory, signed path only.

        Opens the EXISTING bound store with ``mode=rw`` (SQLite never creates a
        file), applies D3 (NORMAL locking, bounded busy timeout) and reads
        back D1 (journal_mode DELETE or WAL) and D2 (synchronous FULL or
        EXTRA) before any statement. Any mismatch or error closes the
        connection and refuses. This is also the single receipts
        fault-injection seam used by tests; callers close the connection on
        every outcome.
        """
        path = self.database_path
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise OfflineAdapterError(f"receipts store unavailable: {type(exc).__name__}") from exc
        if not resolved.is_file():
            raise OfflineAdapterError("receipts store unavailable: not a regular file")
        uri = resolved.as_uri() + "?mode=rw"
        timeout = self._signed_busy_timeout
        connection = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
        try:
            config = _apply_signed_connection_config(connection, timeout)
        except Exception:
            connection.close()
            raise
        self._signed_last_config = config
        return connection

    @staticmethod
    def _signed_rollback(connection) -> bool:
        """True only when rollback() returned normally (J2 confirmed rollback)."""
        try:
            connection.rollback()
            return True
        except Exception:
            return False

    @staticmethod
    def _signed_close(connection) -> None:
        try:
            connection.close()
        except Exception:
            pass

    def _signed_poison_latch(self, step: str, detail: str) -> None:
        if self._signed_poison is None:
            self._signed_poison = f"{step}: {detail}"

    def _signed_fail(self, step, classification, message, *, poison, cause=None):
        if poison:
            self._signed_poison_latch(step, classification)
        error = SignedAdmissionError(
            f"{step}: {message}", step=step, classification=classification, poisoned=poison
        )
        if cause is not None:
            raise error from cause
        raise error

    def _signed_abort_before_commit(self, connection, step, message, exc, *, effect_issued,
                                    owner=False):
        """A failure raised before commit() was invoked.

        Classification per J2: confirmed rolled back only if rollback()
        returned; a failed rollback (RF) is ambiguous. ``effect_issued`` is
        True once this transaction executed a write: (a) then poisons the
        instance (C1, C2). A read-only refusal with a confirmed rollback is
        'refused'; it poisons only on the armed-owner path (``owner``), where
        G is retained and no further call is made.
        """
        if self._signed_rollback(connection):
            if effect_issued:
                self._signed_fail(step, "confirmed_rolled_back", f"{message}; manual recovery",
                                  poison=True, cause=exc)
            self._signed_fail(step, "refused", message, poison=owner, cause=exc)
        self._signed_fail(step, "ambiguous", f"{message}; rollback raised; manual recovery",
                          poison=True, cause=exc)

    def _signed_commit(self, connection, step) -> None:
        """Invoke commit(). Any exception from commit() is ambiguous (J2 (b)/(c))."""
        try:
            connection.commit()
        except Exception as exc:
            self._signed_rollback(connection)
            self._signed_fail(step, "ambiguous", "commit outcome unknown; manual recovery",
                              poison=True, cause=exc)

    def _signed_phase1(self, connection) -> None:
        """C1 phase 1 / P-4: read-only store and refusal gate. Raises _SignedRefusal.

        Never reads ``fail_stop.reason`` (C6, H1 rule 2).
        """
        try:
            marker_table = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'signed_admission_marker'"
            ).fetchone()[0]
            if marker_table != 1:
                raise _SignedRefusal("provisioning marker missing")
            markers = connection.execute(
                "SELECT singleton, marker FROM signed_admission_marker"
            ).fetchall()
            if len(markers) != 1 or markers[0][0] != 1 or markers[0][1] != SIGNED_MARKER_VALUE:
                raise _SignedRefusal("provisioning marker missing or malformed")
            stops = connection.execute("SELECT singleton, stopped FROM fail_stop").fetchall()
            if len(stops) != 1 or stops[0][0] != 1:
                raise _SignedRefusal("fail_stop must hold exactly one singleton row")
            if type(stops[0][1]) is not int or stops[0][1] != 0:
                raise _SignedRefusal("stop present")
            problem = _signed_schema_problem(connection)
            if problem is not None:
                raise _SignedRefusal(f"receipts schema mismatch: {problem}")
            active = connection.execute(
                "SELECT message_id FROM intent_journal WHERE state IN ('PENDING', 'UNCERTAIN')"
            ).fetchall()
            if active:
                raise _SignedRefusal("unresolved intent present")
        except _SignedRefusal:
            raise
        except Exception as exc:
            raise _SignedRefusal(f"receipts store unreadable: {type(exc).__name__}: {exc}") from exc
        try:
            self._reconcile(connection)
        except Exception as exc:
            raise _SignedRefusal(f"reconcile refused: {exc}") from exc
        try:
            self._signed_validate_ledger_records(connection)
        except _SignedRefusal:
            raise
        except Exception as exc:
            raise _SignedRefusal(f"signed record validation failed: {exc}") from exc
        try:
            uncertain = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM audit_receipts WHERE outcome = 'uncertain')"
            ).fetchone()[0]
        except Exception as exc:
            raise _SignedRefusal(f"receipts store unreadable: {type(exc).__name__}") from exc
        if uncertain:
            raise _SignedRefusal(
                "Rule U: landed uncertain audit receipt on the bound store; manual recovery required"
            )

    def _signed_validate_ledger_records(self, connection) -> None:
        """6838692613 4F discriminated signed-record shape validation (signed path only).

        Runs after the unchanged legacy reconcile comparisons. Applies to every
        adapter-origin ledger event that carries ``admission_path``; legacy
        records are left to the legacy rules. Adapter-local consistency only,
        not authentication of SQLite contents.
        """
        journal = dict(
            connection.execute(
                "SELECT message_id, payload_digest FROM intent_journal WHERE state = 'ACCEPTED'"
            ).fetchall()
        )
        with closing(self.ledger._connect()) as ledger_conn:
            rows = ledger_conn.execute(
                "SELECT message_id, event_type, actor, payload FROM events ORDER BY revision"
            ).fetchall()
            ledger_conn.rollback()
        for message_id, event_type, actor, payload_json in rows:
            payload = json.loads(payload_json) if payload_json else {}
            if type(payload) is not dict or payload.get("adapter_origin") != "bridge-02a":
                continue
            if "admission_path" not in payload:
                continue
            problem = _signed_record_problem(payload, event_type, actor)
            if problem is None and journal.get(message_id) != payload.get("digest"):
                problem = "digest does not match journal payload_digest"
            if problem is not None:
                raise _SignedRefusal(f"Derived signed record malformed for {message_id}: {problem}")

    def accept_signed(self, envelope: SignedEnvelope, *, now: datetime | None = None) -> str:
        """J6 signed admission: J3 rows 2a-8 (C1-C6, Rule U). Returns 'accepted' or raises.

        Raises SignedAdmissionError whose ``classification`` is the caller's
        own observation (J2): 'refused' (this call issued no write),
        'rejected' (a definite denial whose single commit was confirmed),
        'stop_retained' (a confirmed S9 HALT record; G kept, manual),
        'confirmed_rolled_back' (condition (a)) or 'ambiguous' (every other
        outcome, including RF). Fresh-process knowledge never upgrades it.
        """
        if self.TRANSPORT_ENABLED or self.execution_enabled:
            raise SignedAdmissionError("External transport is disabled", step="S1",
                                       classification="refused")
        if not getattr(self, "_signed_mode", False):
            raise SignedAdmissionError(
                "signed admission requires OfflineAuthenticatedAdapter.open_signed()",
                step="S2", classification="refused",
            )
        if now is None:
            now = datetime.now(timezone.utc)
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise SignedAdmissionError("now must be a UTC-aware datetime", step="S1",
                                       classification="refused")
        with self._lock:
            if self._signed_poison is not None:
                raise SignedAdmissionError(
                    f"adapter poisoned: {self._signed_poison}; manual restart required",
                    step="S2", classification="refused", poisoned=True,
                )
            return self._accept_signed_locked(envelope, now)

    def _accept_signed_locked(self, envelope, now) -> str:
        attempt = self._signed_t1(envelope, now)
        G = attempt["G"]
        outcome = self._signed_t2(attempt, now)
        if outcome["kind"] == "rejected":
            self._signed_release(G, outcome)
            raise SignedAdmissionError(
                f"S10: admission rejected: {outcome['reason']}", step="S10", classification="rejected"
            )
        self._signed_w2(attempt, now)
        terminal = self._signed_t3(attempt, now)
        self._signed_release(G, terminal)
        return "accepted"

    # --- T1: phases 1-3 in ONE receipts BEGIN IMMEDIATE (C1) -----------------

    def _signed_t1(self, envelope, now) -> dict:
        try:
            connection = self._receipts_connect()
        except Exception as exc:
            self._signed_fail("T1", "refused", f"receipts connection refused: {exc}", poison=False,
                              cause=exc)
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                if getattr(connection, "in_transaction", False):
                    self._signed_abort_before_commit(connection, "T1", f"BEGIN failed: {exc}", exc,
                                                     effect_issued=False)
                self._signed_fail("T1", "refused", f"receipts lock not acquired: {exc}", poison=False,
                                  cause=exc)
            # Phase 1: store and refusal gate, zero writes on refusal.
            try:
                self._signed_phase1(connection)
            except _SignedRefusal as refusal:
                self._signed_abort_before_commit(connection, "T1", f"refused: {refusal}", None,
                                                 effect_issued=False)
            # Phase 2: S4-S7 prechecks; denial branch (E1.b) or refusal.
            try:
                checked = self._signed_phase2(connection, envelope, now)
            except _SignedRefusal as refusal:
                self._signed_abort_before_commit(connection, "T1", f"refused: {refusal}", None,
                                                 effect_issued=False)
            except Exception as exc:
                self._signed_abort_before_commit(connection, "T1", f"precheck failed: {exc}", exc,
                                                 effect_issued=False)
            if checked["denial"] is not None:
                self._signed_denial(connection, checked["snap"], checked["denial"], now)
            # Phase 3: arm S8 (linearization point A).
            snap = checked["snap"]
            fingerprint = checked["F"]
            G = (
                "SIGNED_ADMISSION_IN_PROGRESS v1 "
                f"mid={len(snap.message_id)}:{snap.message_id} "
                f"idem={len(snap.idempotency_key)}:{snap.idempotency_key} "
                f"fp={fingerprint} nonce={secrets.token_hex(16)}"
            )
            try:
                cursor = connection.execute(
                    "UPDATE fail_stop SET stopped = 1, reason = ? WHERE singleton = 1 AND stopped = 0",
                    (G,),
                )
                if cursor.rowcount != 1:
                    raise OfflineAdapterError(f"arm rowcount {cursor.rowcount}")
            except Exception as exc:
                self._signed_abort_before_commit(
                    connection, "A", f"arm failed before commit: {exc}; no reservation attempted", exc,
                    effect_issued=True,
                )
            self._signed_commit(connection, "A")
        finally:
            self._signed_close(connection)
        checked["G"] = G
        return checked

    def _signed_denial(self, connection, snap, denial, now) -> None:
        """E1.b denial branch: exactly one enumerated audit receipt, T1 COMMIT, no arm."""
        outcome, reason = denial
        try:
            self._signed_insert_receipt(connection, snap, outcome, reason, now)
        except Exception as exc:
            self._signed_abort_before_commit(connection, "E1.b", f"denial receipt not written: {exc}",
                                             exc, effect_issued=True)
        self._signed_commit(connection, "E1.b")
        raise SignedAdmissionError(
            f"E1.b: {outcome}: {reason}", step="E1.b", classification="rejected"
        )

    def _signed_phase2(self, connection, envelope, now) -> dict:
        # S4: detach and verify. Failure: refusal, no receipt (4D, Q5).
        snap = _snapshot(envelope)
        if snap is None:
            raise _SignedRefusal("malformed payload")
        problem = _signed_header_problem(snap)
        if problem is not None:
            raise _SignedRefusal(f"malformed payload: {problem}")
        try:
            verdict = verify_signature(snap, self._signed_gate.trust_root, now)
        except Exception as exc:
            raise _SignedRefusal(f"invalid signature: {type(exc).__name__}") from None
        if verdict != "cryptographically_valid":
            raise _SignedRefusal("invalid signature: not cryptographically valid")
        result = {"snap": snap, "denial": None}

        # S5: HALT #1.
        try:
            halted = self._halt_check()
        except Exception as exc:
            result["denial"] = ("uncertain", f"HALT check failed: {type(exc).__name__}")
            return result
        if halted:
            result["denial"] = ("rejected", "HALTED")
            return result

        # Bounded ledger read shared by S6(h) and S7 (6838692613 4A).
        ledger_view = self._signed_ledger_preflight(snap)

        # S6 (a)-(h): each failure is a rejected receipt (E1.b).
        reason = self._signed_s6(connection, snap, now, ledger_view)
        if reason is not None:
            result["denial"] = ("rejected", reason)
            return result

        # S7: mirrored ledger rules. Not enumerated in E1.b, so a refusal
        # writes nothing (C3).
        s7 = _signed_s7_problem(snap, ledger_view)
        if s7 is not None:
            raise _SignedRefusal(f"ledger preflight: {s7}")

        result["F"] = hashlib.sha256(canonical_bytes(snap)).hexdigest()
        result["R0"] = ledger_view["R0"]
        result["captured"] = self._signed_capture(connection, snap)
        return result

    def _signed_ledger_preflight(self, snap) -> dict:
        try:
            with closing(self.ledger._connect()) as ledger_conn:
                ledger_conn.execute("BEGIN")
                try:
                    R0 = self.ledger._current_revision(ledger_conn)
                    p0 = ledger_conn.execute(
                        "SELECT 1 FROM events WHERE message_id = ? LIMIT 2", (snap.message_id,)
                    ).fetchall() + ledger_conn.execute(
                        "SELECT 1 FROM events WHERE idempotency_key = ? LIMIT 2",
                        (snap.idempotency_key,),
                    ).fetchall()
                    rows = ledger_conn.execute(
                        "SELECT event_type, checkpoint_id, payload FROM events "
                        "WHERE project_id = ? AND task_id = ? ORDER BY revision LIMIT ?",
                        (snap.project_id, snap.task_id, MAX_PREFLIGHT_EVENTS + 1),
                    ).fetchall()
                    view = {"R0": R0, "identity_present": bool(p0), "bounded": True,
                            "rows": rows, "state": None, "current_sha": None}
                    if len(rows) > MAX_PREFLIGHT_EVENTS:
                        view["bounded"] = False
                    else:
                        state, current_sha, _ = self.ledger._projected_state(
                            ledger_conn, snap.project_id, snap.task_id
                        )
                        view["state"], view["current_sha"] = state, current_sha
                finally:
                    ledger_conn.rollback()
        except _SignedRefusal:
            raise
        except Exception as exc:
            raise _SignedRefusal(f"ledger preflight unreadable: {type(exc).__name__}") from exc
        if not view["bounded"]:
            raise _SignedRefusal("ledger preflight bound exceeded")
        return view

    def _signed_s6(self, connection, snap, now, ledger_view) -> str | None:
        msg_type = snap.payload.get("type")
        # (a) correction / correction_request and unmapped types.
        if type(msg_type) is not str or msg_type not in SIGNED_EVENT_TYPES:
            return "unsupported or correction type rejected before arming"
        # (b) global replay.
        replay = connection.execute(
            "SELECT 1 FROM audit_receipts WHERE outcome = 'accepted' AND "
            "(message_id = ? OR idempotency_key = ?) LIMIT 1",
            (snap.message_id, snap.idempotency_key),
        ).fetchone() or connection.execute(
            "SELECT 1 FROM intent_journal WHERE message_id = ? OR idempotency_key = ? LIMIT 1",
            (snap.message_id, snap.idempotency_key),
        ).fetchone()
        if replay:
            return "replay: identity already admitted"
        # (c) sequence.
        stream_key = f"{snap.project_id}:{snap.task_id}"
        seq_row = connection.execute(
            "SELECT next_sequence FROM sequence_state WHERE stream_key = ?", (stream_key,)
        ).fetchone()
        expected = seq_row[0] if seq_row else 1
        if snap.sequence != expected:
            return f"sequence {snap.sequence} != {expected}"
        # (d) assignment identity.
        prior = connection.execute(
            "SELECT assignment_id FROM assignment_identity WHERE project_id = ? AND task_id = ?",
            (snap.project_id, snap.task_id),
        ).fetchone()
        if prior and prior[0] != snap.assignment_id:
            return "Assignment identity mismatch"
        # (e) journal checkpoint.
        checkpoints = {
            r[0]
            for r in connection.execute(
                "SELECT DISTINCT checkpoint_id FROM intent_journal WHERE project_id = ? AND task_id = ?",
                (snap.project_id, snap.task_id),
            ).fetchall()
        }
        if checkpoints - {snap.checkpoint_id}:
            return "journal checkpoint mismatch"
        # (f) part 1: an assignment requires the trusted fixture dependency.
        # Part 2 (gate evidence before A) runs last in S6, below.
        if msg_type == "assignment" and not self._trusted_fixture:
            return "Assignment requires trusted fixture"
        # (g) narrowed age bound.
        if now - snap.timestamp > MAX_SIGNED_AGE:
            return "signed message exceeds 3600 s age bound"
        # (h) provenance markers in the stream.
        kinds = set()
        for _event_type, _checkpoint, payload_json in ledger_view["rows"]:
            payload = json.loads(payload_json) if payload_json else {}
            if type(payload) is not dict or payload.get("adapter_origin") != "bridge-02a":
                continue
            if "admission_path" not in payload:
                kinds.add("legacy")
            elif (
                payload.get("admission_path") != SIGNED_ADMISSION_PATH
                or payload.get("digest_kind") != SIGNED_DIGEST_KIND
            ):
                return "provenance: contradictory markers"
            else:
                kinds.add("signed")
        if kinds == {"legacy"}:
            return "provenance: legacy history in stream"
        if len(kinds) > 1:
            return "provenance: mixed stream"
        # (f) part 2 (6838692613 S6(f); ruling C of work order 6869856724): an
        # ASSIGNMENT must also satisfy gate evidence BEFORE A. Evaluated last
        # in S6 so that cheaper local denials never invoke the dependency.
        if msg_type == "assignment":
            return self._signed_assignment_evidence_problem(snap, now)
        return None

    def _signed_assignment_evidence_problem(self, snap, now) -> str | None:
        """Pre-arm assignment evidence: the frozen gate's own mapping, called before A.

        Uses the same configured ``gate.evidence_verifier``, the closed
        AUTHORIZATION_MATRIX tuple and the gate's strict evidence_id and SHA
        rules, and passes exactly the gate's scalars (evidence kind,
        assignment_id, bound SHA, evidence_id, now). The tuple and scalars are
        validated before the call; a valid signature alone never authorizes.
        Rejected, unavailable, non-string or raising results deny here (an
        S6(f) receipt in T1, no arm). This is not a new evidence authority and
        not a reservation: the gate re-verifies authoritatively inside
        ``reserve`` after A, and a pre-arm approval does not make later
        evidence or HALT immutable.
        """
        row = AUTHORIZATION_MATRIX.get((snap.sender, snap.role, snap.recipient, "assignment"))
        if row is None:
            return "Assignment authorization tuple not permitted"
        evidence_kind, sha_field = row
        payload = snap.payload
        problem = _evidence_id_problem(payload)
        if problem is None:
            problem = _sha_problem(payload, sha_field)
        if problem is not None:
            return f"Assignment evidence input invalid: {problem}"
        try:
            verdict = self._signed_gate.evidence_verifier.verify(
                evidence_kind, snap.assignment_id, payload[sha_field], payload["evidence_id"], now
            )
        except Exception:  # noqa: BLE001 - fail closed before A
            return "Assignment evidence verification failed closed"
        if type(verdict) is not str or verdict != "verified":
            shown = verdict if type(verdict) is str and verdict in ("rejected", "unavailable") else "indeterminate"
            return f"Assignment evidence {shown}"
        return None

    def _signed_capture(self, connection, snap) -> tuple:
        """Receipts values revalidated under T2 (J3 row 4)."""
        stream_key = f"{snap.project_id}:{snap.task_id}"
        return (
            connection.execute(
                "SELECT stream_key, next_sequence FROM sequence_state WHERE stream_key = ?", (stream_key,)
            ).fetchall(),
            connection.execute(
                "SELECT project_id, task_id, assignment_id FROM assignment_identity "
                "WHERE project_id = ? AND task_id = ?",
                (snap.project_id, snap.task_id),
            ).fetchall(),
            connection.execute(
                "SELECT intent_id, message_id, idempotency_key, state FROM intent_journal "
                "WHERE message_id = ? OR idempotency_key = ? OR (project_id = ? AND task_id = ?) "
                "ORDER BY intent_id",
                (snap.message_id, snap.idempotency_key, snap.project_id, snap.task_id),
            ).fetchall(),
            connection.execute(
                "SELECT receipt_id FROM audit_receipts WHERE outcome = 'accepted' AND "
                "(message_id = ? OR idempotency_key = ?) ORDER BY receipt_id",
                (snap.message_id, snap.idempotency_key),
            ).fetchall(),
        )

    def _signed_owner_stop_problem(self, connection, G) -> str | None:
        """Armed-owner predicate: exactly one row, stopped = 1 AND reason = in-memory G."""
        rows = connection.execute("SELECT singleton, stopped, reason FROM fail_stop").fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            return "fail_stop row missing or duplicated"
        if rows[0][1] != 1 or type(rows[0][2]) is not str or rows[0][2] != G:
            return "exact-G predicate false (foreign, stale or released stop)"
        return None

    # --- T2: S9 HALT #2, gate reserve, read-back, W1 (no clear) -------------

    def _signed_t2(self, attempt, now) -> dict:
        snap, G = attempt["snap"], attempt["G"]
        try:
            connection = self._receipts_connect()
        except Exception as exc:
            self._signed_fail("T2", "refused", f"receipts connection refused: {exc}; G retained",
                              poison=True, cause=exc)
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                if getattr(connection, "in_transaction", False):
                    self._signed_abort_before_commit(connection, "T2", f"BEGIN failed: {exc}", exc,
                                                     effect_issued=False, owner=True)
                self._signed_fail("T2", "refused", f"receipts lock not acquired: {exc}; G retained",
                                  poison=True, cause=exc)
            halt_state = None
            try:
                problem = self._signed_owner_stop_problem(connection, G)
                if problem is None and connection.execute(
                    "SELECT COUNT(*) FROM signed_admission_marker"
                ).fetchone()[0] != 1:
                    problem = "provisioning marker missing"
                if problem is None and connection.execute(
                    "SELECT 1 FROM intent_journal WHERE state IN ('PENDING', 'UNCERTAIN') LIMIT 1"
                ).fetchone():
                    problem = "unresolved intent present"
                if problem is None and self._signed_capture(connection, snap) != attempt["captured"]:
                    problem = "captured receipts state changed"
                if problem is None and (
                    hashlib.sha256(canonical_bytes(snap)).hexdigest() != attempt["F"]
                    or hashlib.sha256(_canonical_json(snap.payload)).digest() != snap.payload_digest
                ):
                    problem = "fingerprint changed"
                if problem is None:
                    try:
                        halt_state = bool(self._halt_check())
                    except Exception as exc:
                        problem = f"HALT #2 check raised: {type(exc).__name__}"
            except Exception as exc:
                problem = f"revalidation unreadable: {type(exc).__name__}"
            if problem is not None:
                # Owner step refuses with no write; G is never touched here.
                self._signed_abort_before_commit(connection, "T2", f"{problem}; G retained", None,
                                                 effect_issued=False, owner=True)
            if halt_state:
                self._signed_s9_halt(connection, snap, G, now)
            # S10: gate reservation while the receipts lock is held.
            try:
                res = self._signed_gate.reserve(snap, now)
                if (
                    type(res) is not ReservationResult
                    or type(res.outcome) is not str
                    or type(res.reason) is not str
                    or res.outcome not in ("reserved", "rejected")
                ):
                    raise OfflineAdapterError("admission indeterminate/orphan result")
            except Exception as exc:
                # Whole-call gate outcome is ambiguous (C2): no W1, no receipt,
                # no release; G stays on both branches.
                self._signed_rollback(connection)
                self._signed_fail("S10", "ambiguous",
                                  f"admission indeterminate/orphan; guard active; manual review ({exc})",
                                  poison=True, cause=exc)
            if res.outcome == "rejected":
                try:
                    receipt_id = self._signed_insert_receipt(
                        connection, snap, "rejected", f"admission: {res.reason}", now
                    )
                except Exception as exc:
                    self._signed_abort_before_commit(connection, "S10", f"rejection receipt failed: {exc}",
                                                     exc, effect_issued=True)
                self._signed_commit(connection, "S10")
                return {"kind": "rejected", "reason": res.reason, "receipt_id": receipt_id,
                        "message_id": snap.message_id}
            # S11: post-gate binding and stored-row consistency check (4C).
            try:
                if hashlib.sha256(canonical_bytes(snap)).hexdigest() != attempt["F"]:
                    raise OfflineAdapterError("fingerprint changed after reserve")
                self._signed_reservation_readback(snap)
            except Exception as exc:
                self._signed_abort_before_commit(connection, "S11",
                                                 f"reservation read-back failed: {exc}; G retained",
                                                 exc, effect_issued=False, owner=True)
            # W1: PENDING intent; T2 COMMIT. No clear.
            try:
                connection.execute(
                    """
                    INSERT INTO intent_journal (
                        message_id, idempotency_key, project_id, task_id,
                        checkpoint_id, assignment_id, sequence, payload_digest,
                        state, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                    """,
                    (
                        snap.message_id, snap.idempotency_key, snap.project_id, snap.task_id,
                        snap.checkpoint_id, snap.assignment_id, snap.sequence,
                        snap.payload_digest.hex(), now.isoformat(),
                    ),
                )
            except Exception as exc:
                self._signed_abort_before_commit(connection, "W1", f"PENDING not written: {exc}", exc,
                                                 effect_issued=True)
            self._signed_commit(connection, "W1")
            return {"kind": "reserved"}
        finally:
            self._signed_close(connection)

    def _signed_s9_halt(self, connection, snap, G, now) -> None:
        """S9 HALT #2 (O8): one rejected receipt carrying the explanation; G kept; manual stop.

        C2: this transaction never writes ``fail_stop``; G stays byte-equal.
        """
        try:
            self._signed_insert_receipt(
                connection, snap, "rejected", "HALTED at reacquisition (S9); guard retained", now
            )
        except Exception as exc:
            self._signed_abort_before_commit(connection, "S9", f"HALT record failed: {exc}", exc,
                                             effect_issued=True)
        self._signed_commit(connection, "S9")
        self._signed_fail("S9", "stop_retained", "HALT at reacquisition; stop kept; manual recovery",
                          poison=True)

    def _signed_reservation_readback(self, snap) -> None:
        """4C exact read-only projection: at most two rows, exactly one required."""
        gate_path = Path(self._signed_gate.database_path).resolve(strict=True)
        uri = gate_path.as_uri() + "?mode=ro"
        reader = sqlite3.connect(uri, uri=True, timeout=self._signed_busy_timeout)
        try:
            reader.execute("BEGIN")
            rows = reader.execute(
                """
                SELECT id, project_id, task_id, checkpoint_id, assignment_id,
                       message_id, idempotency_key, sender, recipient, role,
                       payload_digest, sequence, state, recorded_at
                FROM reservations
                WHERE project_id = ? AND task_id = ?
                  AND (message_id = ? OR idempotency_key = ?)
                LIMIT 2
                """,
                (snap.project_id, snap.task_id, snap.message_id, snap.idempotency_key),
            ).fetchall()
            reader.rollback()
        finally:
            reader.close()
        if len(rows) != 1:
            raise OfflineAdapterError(f"expected exactly one reservation row, found {len(rows)}")
        (rid, project, task, checkpoint, assignment, message_id, idem, sender, recipient, role,
         digest, sequence, state, recorded_at) = rows[0]
        expected = (
            (project, snap.project_id), (task, snap.task_id), (checkpoint, snap.checkpoint_id),
            (assignment, snap.assignment_id), (message_id, snap.message_id),
            (idem, snap.idempotency_key), (sender, snap.sender), (recipient, snap.recipient),
            (role, snap.role),
        )
        for stored, wanted in expected:
            if type(stored) is not str or stored != wanted:
                raise OfflineAdapterError("reservation row does not match the snapshot")
        if type(digest) is not bytes or digest != snap.payload_digest:
            raise OfflineAdapterError("reservation payload_digest mismatch")
        if type(sequence) is not int or sequence != snap.sequence:
            raise OfflineAdapterError("reservation sequence mismatch")
        if type(state) is not str or state != "RESERVED":
            raise OfflineAdapterError("reservation state is not RESERVED")
        if type(rid) is not int or rid <= 0:
            raise OfflineAdapterError("reservation id malformed")
        if type(recorded_at) is not str:
            raise OfflineAdapterError("reservation recorded_at malformed")
        try:
            parsed = datetime.fromisoformat(recorded_at)
        except ValueError:
            raise OfflineAdapterError("reservation recorded_at malformed") from None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise OfflineAdapterError("reservation recorded_at malformed")

    # --- W2: ledger append, no receipts lock held ---------------------------

    def _signed_w2(self, attempt, now) -> None:
        snap = attempt["snap"]
        msg_type = snap.payload["type"]
        evidence_kind, sha_field = AUTHORIZATION_MATRIX[(snap.sender, snap.role, snap.recipient, msg_type)]
        record = {
            "adapter_origin": "bridge-02a",
            "admission_path": SIGNED_ADMISSION_PATH,
            "synthetic": True,
            "schema_version": snap.schema_version,
            "digest": snap.payload_digest.hex(),
            "digest_kind": SIGNED_DIGEST_KIND,
            "type": msg_type,
            "assignment_id": snap.assignment_id,
            "recipient": snap.recipient,
            "role": snap.role,
            "evidence_kind": evidence_kind,
            "evidence_id": snap.payload["evidence_id"],
        }
        if sha_field == "base_sha":
            record["base_sha"] = snap.payload["base_sha"]
        else:
            record["implementation_sha"] = snap.payload["implementation_sha"]
            record["sha"] = snap.payload["implementation_sha"]
            record["sha_derived_from"] = "implementation_sha"
        try:
            revision = self.ledger.record_event(
                event_type=SIGNED_EVENT_TYPES[msg_type],
                project_id=snap.project_id,
                task_id=snap.task_id,
                checkpoint_id=snap.checkpoint_id,
                actor=snap.sender,
                payload=record,
                message_id=snap.message_id,
                idempotency_key=snap.idempotency_key,
                expected_revision=attempt["R0"],
                now=now,
            )
        except Exception as exc:
            # Whole-call ledger outcome: ambiguous; G and PENDING stay (C2).
            self._signed_fail("W2", "ambiguous", f"ledger append not confirmed: {exc}; G and PENDING kept",
                              poison=True, cause=exc)
        if type(revision) is not int or revision != attempt["R0"] + 1:
            self._signed_fail("W2", "ambiguous", f"ledger returned revision {revision!r}, expected R0 + 1",
                              poison=True)

    # --- T3: W3 (no clear) -----------------------------------------------------

    def _signed_t3(self, attempt, now) -> dict:
        snap, G = attempt["snap"], attempt["G"]
        try:
            connection = self._receipts_connect()
        except Exception as exc:
            self._signed_fail("T3", "refused", f"receipts connection refused: {exc}; G retained",
                              poison=True, cause=exc)
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                if getattr(connection, "in_transaction", False):
                    self._signed_abort_before_commit(connection, "T3", f"BEGIN failed: {exc}", exc,
                                                     effect_issued=False, owner=True)
                self._signed_fail("T3", "refused", f"receipts lock not acquired: {exc}; G retained",
                                  poison=True, cause=exc)
            try:
                problem = self._signed_owner_stop_problem(connection, G)
                if problem is None:
                    intent = connection.execute(
                        "SELECT state, idempotency_key, project_id, task_id, checkpoint_id, assignment_id, "
                        "sequence, payload_digest FROM intent_journal WHERE message_id = ?",
                        (snap.message_id,),
                    ).fetchall()
                    if intent != [(
                        "PENDING", snap.idempotency_key, snap.project_id, snap.task_id,
                        snap.checkpoint_id, snap.assignment_id, snap.sequence, snap.payload_digest.hex(),
                    )]:
                        problem = "PENDING intent for this identity not found"
            except Exception as exc:
                problem = f"revalidation unreadable: {type(exc).__name__}"
            if problem is not None:
                self._signed_abort_before_commit(connection, "T3", f"{problem}; G retained", None,
                                                 effect_issued=False, owner=True)
            try:
                stream_key = f"{snap.project_id}:{snap.task_id}"
                if snap.sequence == 1 and connection.execute(
                    "SELECT 1 FROM sequence_state WHERE stream_key = ?", (stream_key,)
                ).fetchone() is None:
                    cursor = connection.execute(
                        "INSERT INTO sequence_state (stream_key, next_sequence) VALUES (?, ?)",
                        (stream_key, snap.sequence + 1),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE sequence_state SET next_sequence = ? "
                        "WHERE stream_key = ? AND next_sequence = ?",
                        (snap.sequence + 1, stream_key, snap.sequence),
                    )
                if cursor.rowcount != 1:
                    raise OfflineAdapterError(f"sequence rowcount {cursor.rowcount}")
                cursor = connection.execute(
                    "UPDATE intent_journal SET state = 'ACCEPTED' WHERE message_id = ? AND state = 'PENDING'",
                    (snap.message_id,),
                )
                if cursor.rowcount != 1:
                    raise OfflineAdapterError(f"intent rowcount {cursor.rowcount}")
                receipt_id = self._signed_insert_receipt(connection, snap, "accepted", None, now)
                if snap.payload["type"] == "assignment":
                    connection.execute(
                        "INSERT INTO assignment_identity (project_id, task_id, assignment_id) VALUES (?, ?, ?)",
                        (snap.project_id, snap.task_id, snap.assignment_id),
                    )
            except Exception as exc:
                self._signed_abort_before_commit(connection, "W3", f"W3 not written: {exc}", exc,
                                                 effect_issued=True)
            self._signed_commit(connection, "W3")
            return {"kind": "accepted", "receipt_id": receipt_id, "message_id": snap.message_id}
        finally:
            self._signed_close(connection)

    # --- R: separate exact-G release (C5, E2) ----------------------------------

    def _signed_release(self, G, terminal) -> None:
        try:
            connection = self._receipts_connect()
        except Exception as exc:
            self._signed_fail("R", "refused", f"receipts connection refused: {exc}; G retained",
                              poison=True, cause=exc)
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except Exception as exc:
                if getattr(connection, "in_transaction", False):
                    self._signed_abort_before_commit(connection, "R", f"BEGIN failed: {exc}", exc,
                                                     effect_issued=False, owner=True)
                self._signed_fail("R", "refused", f"receipts lock not acquired: {exc}; G retained",
                                  poison=True, cause=exc)
            try:
                problem = self._signed_owner_stop_problem(connection, G)
                if problem is None:
                    receipt = connection.execute(
                        "SELECT message_id, outcome FROM audit_receipts WHERE receipt_id = ?",
                        (terminal["receipt_id"],),
                    ).fetchall()
                    if receipt != [(terminal["message_id"], terminal["kind"])]:
                        problem = "terminal receipt not present"
                if problem is None and terminal["kind"] == "accepted":
                    intent = connection.execute(
                        "SELECT state FROM intent_journal WHERE message_id = ?", (terminal["message_id"],)
                    ).fetchall()
                    if intent != [("ACCEPTED",)]:
                        problem = "terminal ACCEPTED intent not present"
            except Exception as exc:
                problem = f"revalidation unreadable: {type(exc).__name__}"
            if problem is not None:
                self._signed_abort_before_commit(connection, "R", f"{problem}; G retained", None,
                                                 effect_issued=False, owner=True)
            try:
                cursor = connection.execute(
                    "UPDATE fail_stop SET stopped = 0, reason = NULL "
                    "WHERE singleton = 1 AND stopped = 1 AND reason = ?",
                    (G,),
                )
                if cursor.rowcount != 1:
                    raise OfflineAdapterError(f"release rowcount {cursor.rowcount}")
            except Exception as exc:
                self._signed_abort_before_commit(connection, "R", f"release not confirmed: {exc}", exc,
                                                 effect_issued=True)
            self._signed_commit(connection, "R")
        finally:
            self._signed_close(connection)

    def _signed_insert_receipt(self, connection, snap, outcome, reason, now) -> int:
        cursor = connection.execute(
            """
            INSERT INTO audit_receipts (
                message_id, idempotency_key, project_id, task_id,
                checkpoint_id, assignment_id, sender, outcome, reason, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap.message_id, snap.idempotency_key, snap.project_id, snap.task_id,
                snap.checkpoint_id, snap.assignment_id, snap.sender, outcome, reason, now.isoformat(),
            ),
        )
        if cursor.rowcount != 1:
            raise OfflineAdapterError(f"receipt rowcount {cursor.rowcount}")
        return cursor.lastrowid


# ----------------------------------------------------------------------------
# BRIDGE-02B.2.2 J6 offline disposable-store prototype (work order 6868633069):
# module-level constants, validators and the P-3 manual provisioning entry point.
# OFFLINE SYNTHETIC ONLY. execution_enabled=False. No recovery API.
# ----------------------------------------------------------------------------

SIGNED_MARKER_VALUE = "QLA-BRIDGE-02B.2.2-J6 signed-admission receipts store v1 (offline synthetic)"
SIGNED_ALLOWED_JOURNAL_MODES = ("delete", "wal")
SIGNED_SYNCHRONOUS_FULL = 2
SIGNED_SYNCHRONOUS_EXTRA = 3
SIGNED_MAX_BUSY_TIMEOUT = 30.0
MAX_PREFLIGHT_EVENTS = 1024
MAX_SIGNED_AGE = timedelta(seconds=3600)
SIGNED_ADMISSION_PATH = "bridge-02b-signed"
SIGNED_DIGEST_KIND = "sha256-canonical-02b"
SIGNED_EVENT_TYPES = {
    "assignment": EventType.ASSIGNMENT_CREATED,
    "implementation": EventType.IMPLEMENTATION_SUBMITTED,
    "review": EventType.REVIEW_RECORDED,
}
_SIGNED_COMMON_KEYS = frozenset(
    {
        "adapter_origin", "admission_path", "synthetic", "schema_version", "digest",
        "digest_kind", "type", "assignment_id", "recipient", "role", "evidence_kind",
        "evidence_id",
    }
)
_SIGNED_KEYS_BY_TYPE = {
    "assignment": _SIGNED_COMMON_KEYS | {"base_sha"},
    "implementation": _SIGNED_COMMON_KEYS | {"implementation_sha", "sha", "sha_derived_from"},
    "review": _SIGNED_COMMON_KEYS | {"implementation_sha", "sha", "sha_derived_from"},
}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SIGNED_STR_HEADERS = (
    "sender", "recipient", "role", "project_id", "task_id", "checkpoint_id",
    "assignment_id", "message_id", "idempotency_key", "key_id",
)

# P-9 (work order 6870819489, amended 6871180075): the legacy _init_receipts
# statements, unchanged in bytes and order, executed by the guarded
# constructor inside one receipts BEGIN IMMEDIATE transaction.
_LEGACY_INIT_STATEMENTS = (
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
                """,
    """
                CREATE TABLE IF NOT EXISTS sequence_state (
                    stream_key TEXT PRIMARY KEY,
                    next_sequence INTEGER NOT NULL
                )
                """,
    """
                CREATE TABLE IF NOT EXISTS intent_journal (
                    intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """,
    """
                CREATE TABLE IF NOT EXISTS fail_stop (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    stopped INTEGER NOT NULL,
                    reason TEXT
                )
                """,
    "INSERT OR IGNORE INTO fail_stop (singleton, stopped, reason) VALUES (1, 0, NULL)",
    """
                CREATE TABLE IF NOT EXISTS assignment_identity (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    PRIMARY KEY (project_id, task_id)
                )
                """,
    """
                CREATE TRIGGER IF NOT EXISTS audit_receipts_no_update
                BEFORE UPDATE ON audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'audit_receipts is append-only');
                END
                """,
    """
                CREATE TRIGGER IF NOT EXISTS audit_receipts_no_delete
                BEFORE DELETE ON audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'audit_receipts is append-only');
                END
                """,
)

# Reserved schema-object name (any object kind, compared case-insensitively).
_SIGNED_MARKER_OBJECT = "signed_admission_marker"


def _legacy_init_connect(database_path):
    """Private P-9 connection seam (test fault injection only; not a public interface).

    Plain ``sqlite3.connect`` as the legacy constructor used it (a missing file
    is still created; default 5 s busy timeout), with explicit transaction
    control so no statement can autocommit outside BEGIN IMMEDIATE.
    """
    return sqlite3.connect(database_path, timeout=5.0, isolation_level=None)


# Receipts DDL: byte-for-byte the statements of the legacy _init_receipts
# (existing schema), plus the one new J6 table (schema change; fresh
# disposable stores only, never a migration of an existing store).
_SIGNED_RECEIPTS_DDL = (
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
                """,
    """
                CREATE TABLE IF NOT EXISTS sequence_state (
                    stream_key TEXT PRIMARY KEY,
                    next_sequence INTEGER NOT NULL
                )
                """,
    """
                CREATE TABLE IF NOT EXISTS intent_journal (
                    intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """,
    """
                CREATE TABLE IF NOT EXISTS fail_stop (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    stopped INTEGER NOT NULL,
                    reason TEXT
                )
                """,
    """
                CREATE TABLE IF NOT EXISTS assignment_identity (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    PRIMARY KEY (project_id, task_id)
                )
                """,
    """
                CREATE TRIGGER IF NOT EXISTS audit_receipts_no_update
                BEFORE UPDATE ON audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'audit_receipts is append-only');
                END
                """,
    """
                CREATE TRIGGER IF NOT EXISTS audit_receipts_no_delete
                BEFORE DELETE ON audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'audit_receipts is append-only');
                END
                """,
    """
                CREATE TABLE signed_admission_marker (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    marker TEXT NOT NULL,
                    provisioned_at TEXT NOT NULL
                )
                """,
)


def _normalize_ddl(sql: str) -> str:
    text = " ".join(sql.split())
    for prefix in ("CREATE TABLE IF NOT EXISTS ", "CREATE TRIGGER IF NOT EXISTS "):
        if text.startswith(prefix):
            text = prefix.replace("IF NOT EXISTS ", "") + text[len(prefix):]
    return text


def _expected_signed_schema() -> dict:
    expected = {}
    for statement in _SIGNED_RECEIPTS_DDL:
        text = _normalize_ddl(statement)
        kind = "trigger" if text.startswith("CREATE TRIGGER") else "table"
        name = text.split()[2]
        expected[(kind, name)] = text
    return expected


def _signed_schema_problem(connection) -> str | None:
    """Exact provisioned schema: the legacy receipts objects plus the marker table."""
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'trigger') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    actual = {}
    for kind, name, sql in rows:
        if (kind, name) in actual or type(sql) is not str:
            return f"duplicate or unreadable object {name}"
        actual[(kind, name)] = _normalize_ddl(sql)
    expected = _expected_signed_schema()
    if set(actual) != set(expected):
        return "unexpected or missing schema objects"
    for key, text in expected.items():
        if actual[key] != text:
            return f"definition of {key[1]} differs"
    return None


def _apply_signed_connection_config(connection, timeout: float) -> dict:
    """D3 then D1/D2 read-back; raises OfflineAdapterError on any mismatch."""
    locking = connection.execute("PRAGMA locking_mode = NORMAL").fetchone()
    if locking is None or str(locking[0]).lower() != "normal":
        raise OfflineAdapterError(f"D3 locking_mode read-back {locking!r}")
    busy = connection.execute("PRAGMA busy_timeout").fetchone()
    if busy is None or busy[0] != int(timeout * 1000):
        raise OfflineAdapterError(f"D3 busy_timeout read-back {busy!r}")
    mode = connection.execute("PRAGMA journal_mode").fetchone()
    if mode is None or str(mode[0]).lower() not in SIGNED_ALLOWED_JOURNAL_MODES:
        raise OfflineAdapterError(f"D1 journal_mode read-back {mode!r}")
    connection.execute("PRAGMA synchronous = FULL")
    sync = connection.execute("PRAGMA synchronous").fetchone()
    if sync is None or sync[0] not in (SIGNED_SYNCHRONOUS_FULL, SIGNED_SYNCHRONOUS_EXTRA):
        raise OfflineAdapterError(f"D2 synchronous read-back {sync!r}")
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "journal_mode": str(mode[0]).lower(),
        "synchronous": sync[0],
        "locking_mode": str(locking[0]).lower(),
        "busy_timeout_ms": busy[0],
    }


def _signed_store_path(database_path, step: str) -> Path:
    if isinstance(database_path, bool) or not isinstance(database_path, (str, os.PathLike)):
        raise SignedAdmissionError("receipts path must be a str or os.PathLike", step=step,
                                   classification="refused")
    text = os.fspath(database_path)
    if type(text) is not str or not text or "\x00" in text or len(text) > 4096:
        raise SignedAdmissionError("receipts path is empty or malformed", step=step,
                                   classification="refused")
    if text.startswith("file:") or text == ":memory:":
        raise SignedAdmissionError("receipts path must be a plain filesystem path", step=step,
                                   classification="refused")
    return Path(text)


def _signed_header_problem(snap: SignedEnvelope) -> str | None:
    """6838692613 2.6: exact immutable builtin header types on the detached snapshot."""
    for name in _SIGNED_STR_HEADERS:
        if type(getattr(snap, name)) is not str:
            return f"{name} must be str"
    for name in ("schema_version", "sequence"):
        if type(getattr(snap, name)) is not int:
            return f"{name} must be int"
    for name in ("payload_digest", "signature"):
        if type(getattr(snap, name)) is not bytes:
            return f"{name} must be bytes"
    for name in ("timestamp", "expiry"):
        value = getattr(snap, name)
        if type(value) is not datetime or value.tzinfo is not timezone.utc:
            return f"{name} must be a builtin UTC datetime"
    return None


def _signed_record_problem(payload: dict, event_type, actor) -> str | None:
    """4F closed key sets, exact types, markers, matrix tuple and event mapping."""
    if payload.get("admission_path") != SIGNED_ADMISSION_PATH:
        return "admission_path"
    if payload.get("digest_kind") != SIGNED_DIGEST_KIND:
        return "digest_kind"
    msg_type = payload.get("type")
    if type(msg_type) is not str or msg_type not in _SIGNED_KEYS_BY_TYPE:
        return "type"
    if set(payload) != _SIGNED_KEYS_BY_TYPE[msg_type]:
        return "closed key set"
    if event_type != SIGNED_EVENT_TYPES[msg_type].value:
        return "event mapping"
    if payload["synthetic"] is not True:
        return "synthetic"
    if type(payload["schema_version"]) is not int:
        return "schema_version"
    for key in ("assignment_id", "recipient", "role", "evidence_kind", "evidence_id"):
        if type(payload[key]) is not str or not payload[key]:
            return key
    if type(payload["digest"]) is not str or _HEX64.fullmatch(payload["digest"]) is None:
        return "digest"
    matrix = AUTHORIZATION_MATRIX.get((actor, payload["role"], payload["recipient"], msg_type))
    if matrix is None or matrix[0] != payload["evidence_kind"]:
        return "authorization tuple"
    sha_field = matrix[1]
    value = payload.get(sha_field)
    if type(value) is not str or _SHA40.fullmatch(value) is None:
        return sha_field
    if sha_field == "implementation_sha":
        if payload["sha"] != value or payload["sha_derived_from"] != "implementation_sha":
            return "derived sha"
    return None


def _signed_s7_problem(snap, view) -> str | None:
    """S7 mirrored ledger rules (6838692613 4A P0-P6). Advisory; W2 stays authoritative."""
    if view["identity_present"]:
        return "P0 identity already present in ledger"
    state, current_sha = view["state"], view["current_sha"]
    event_type = SIGNED_EVENT_TYPES[snap.payload["type"]]
    if state is not None and view["rows"] and view["rows"][-1][1] != snap.checkpoint_id:
        return "P2 inconsistent checkpoint identifier"
    if event_type == EventType.ASSIGNMENT_CREATED and state is not None:
        return "P3 conflicting assignment"
    if state in {CheckpointState.HALTED, CheckpointState.UNCERTAIN}:
        return "P4 stream HALTED or UNCERTAIN"
    if event_type not in ALLOWED_TRANSITIONS.get(state, set()):
        return f"P5 illegal transition from {state}"
    if event_type == EventType.IMPLEMENTATION_SUBMITTED:
        if state == CheckpointState.CORRECTION_REQUESTED and snap.payload.get("implementation_sha") == current_sha:
            return "stale implementation report"
    if event_type == EventType.REVIEW_RECORDED and snap.payload.get("implementation_sha") != current_sha:
        return "P6 review SHA does not match current implementation"
    return None


def provision_signed_receipts_store(database_path, *, journal_mode: str = "delete") -> dict:
    """P-3 manual provisioning of a NEW disposable signed-admission receipts store.

    Manual operator/test step only: never called by any admission or
    constructor path. The target is created atomically with O_CREAT|O_EXCL,
    so an existing path is refused without an exists()-then-create race; an
    existing -journal/-wal/-shm sidecar is also refused (orphans are never
    adopted). Writes the existing receipts schema, the signed_admission_marker
    row and fail_stop (1, 0, NULL) in one BEGIN IMMEDIATE transaction after
    setting D1 and D2. No overwrite, reinitialization or migration. On any
    failure after the file was created, the unmarked file is left in place
    for manual inspection (admission refuses it: marker missing). The marker
    proves provisioning only, not store identity or freshness.
    """
    path = _signed_store_path(database_path, "provision")
    if type(journal_mode) is not str or journal_mode not in SIGNED_ALLOWED_JOURNAL_MODES:
        raise SignedAdmissionError("journal_mode must be 'delete' or 'wal'", step="provision",
                                   classification="refused")
    parent = path.parent
    if not parent.is_dir():
        raise SignedAdmissionError("parent directory does not exist", step="provision",
                                   classification="refused")
    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.lexists(str(path) + suffix):
            raise SignedAdmissionError(f"orphan sidecar {suffix} present; refusing", step="provision",
                                       classification="refused")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        raise SignedAdmissionError("target exists; provisioning refuses existing stores",
                                   step="provision", classification="refused") from None
    except OSError as exc:
        raise SignedAdmissionError(f"target not creatable: {type(exc).__name__}", step="provision",
                                   classification="refused") from exc
    os.close(fd)
    left = "the newly created unmarked disposable file is left in place for manual inspection"
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=rw"
        connection = _provision_connect(uri)
    except Exception as exc:
        raise SignedAdmissionError(
            f"provisioning failed before any transaction (connection): {type(exc).__name__}; {left}",
            step="provision", classification="pre_transaction_failure",
        ) from exc
    try:
        phase = "configure"
        try:
            mode = connection.execute(f"PRAGMA journal_mode = {journal_mode.upper()}").fetchone()
            if mode is None or str(mode[0]).lower() != journal_mode:
                raise OfflineAdapterError(f"D1 journal_mode not applied: {mode!r}")
            config = _apply_signed_connection_config(connection, 5.0)
            phase = "begin"
            connection.execute("BEGIN IMMEDIATE")
            phase = "transaction"
            existing = connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
            if existing != 0:
                raise OfflineAdapterError("target is not an empty new store")
            for statement in _SIGNED_RECEIPTS_DDL:
                connection.execute(statement)
            connection.execute("INSERT INTO fail_stop (singleton, stopped, reason) VALUES (1, 0, NULL)")
            connection.execute(
                "INSERT INTO signed_admission_marker (singleton, marker, provisioned_at) VALUES (1, ?, ?)",
                (SIGNED_MARKER_VALUE, datetime.now(timezone.utc).isoformat()),
            )
            problem = _signed_schema_problem(connection)
            if problem is not None:
                raise OfflineAdapterError(f"provisioned schema check failed: {problem}")
        except Exception as exc:
            active = True
            if phase != "transaction":
                try:
                    active = bool(connection.in_transaction)
                except Exception:  # noqa: BLE001 - unknown: treat as possibly active
                    active = True
            if not active:
                # No transaction was ever active: rollback is not invoked and
                # nothing is claimed about one. D1 may already be applied to
                # the new file (journal_mode is persistent for WAL).
                raise SignedAdmissionError(
                    f"provisioning failed before any transaction ({phase}): {exc}; {left}",
                    step="provision", classification="pre_transaction_failure",
                ) from exc
            # commit() was never invoked: confirmed rolled back only when
            # rollback() actually returned; otherwise ambiguous (J2, RF).
            try:
                connection.rollback()
                rolled_back = True
            except Exception:  # noqa: BLE001
                rolled_back = False
            if rolled_back:
                raise SignedAdmissionError(
                    f"provisioning failed before commit and rollback returned: {exc}; {left}",
                    step="provision", classification="confirmed_rolled_back",
                ) from exc
            raise SignedAdmissionError(
                f"provisioning failed before commit; rollback raised; outcome unknown: {exc}; {left}",
                step="provision", classification="ambiguous",
            ) from exc
        try:
            connection.commit()
        except Exception as exc:
            raise SignedAdmissionError(
                f"provisioning commit outcome unknown: {exc}; inspect manually",
                step="provision", classification="ambiguous",
            ) from exc
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - cleanup only; outcome already classified
            pass
    return config


def _provision_connect(uri: str):
    """Private P-3 connection seam (test fault injection only; not a public interface)."""
    return sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
