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

import json
import sqlite3
import threading
from contextlib import closing
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
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
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
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fail_stop (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    stopped INTEGER NOT NULL,
                    reason TEXT
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO fail_stop (singleton, stopped, reason) VALUES (1, 0, NULL)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assignment_identity (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    PRIMARY KEY (project_id, task_id)
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
