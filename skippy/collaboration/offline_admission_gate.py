"""
SKIPPY AI Collaboration Bridge — BRIDGE-02B.2.1 Offline Admission Gate.

OFFLINE SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Durable reservation of message identity. Does not write the protected
BRIDGE-02A ledger. Does not modify the BRIDGE-02B.1 verifier.

A caller-supplied verified flag is never authorization.
execution_enabled remains False. External transport remains disabled.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from skippy.collaboration.offline_signature_verifier import (
    SignatureError,
    SignedEnvelope,
    TrustRoot,
    _canonical_json,
    verify_signature,
)

ALLOWED_TYPES = frozenset({"assignment", "implementation", "correction", "review"})

# Closed authorization matrix: (sender, role, recipient, type) -> (evidence kind, bound SHA field).
# Any tuple not listed here is rejected. Signature, matrix and verified evidence are
# three separate requirements.
AUTHORIZATION_MATRIX: dict[tuple[str, str, str, str], tuple[str, str]] = {
    ("chief", "chief", "jason", "assignment"): ("chief_authorization", "base_sha"),
    ("jason", "implementer", "skippy", "implementation"): (
        "chief_authorization",
        "implementation_sha",
    ),
    ("jason", "implementer", "skippy", "correction"): (
        "chief_authorization",
        "implementation_sha",
    ),
    ("skippy", "reviewer", "jason", "review"): ("skippy_review", "implementation_sha"),
}

SHA_FIELDS = ("base_sha", "implementation_sha")
ADAPTER_SHA_ALIAS = "sha"
# Full 40-character lowercase Git SHA-1. Used with re.fullmatch (never match/$),
# so a trailing newline is rejected. Format validation does not prove a commit exists.
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")

SCHEMA_VERSION = 2

_TABLE_DDL = """
CREATE TABLE reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    role TEXT NOT NULL,
    payload_digest BLOB NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""

_INDEX_DDL = (
    "CREATE UNIQUE INDEX uq_message ON reservations(project_id, task_id, message_id)",
    "CREATE UNIQUE INDEX uq_idem ON reservations(project_id, task_id, idempotency_key)",
)

# PRAGMA table_xinfo rows without cid: (name, declared type, notnull, default, pk, hidden).
_EXPECTED_COLUMNS = (
    ("id", "INTEGER", 0, None, 1, 0),
    ("project_id", "TEXT", 1, None, 0, 0),
    ("task_id", "TEXT", 1, None, 0, 0),
    ("checkpoint_id", "TEXT", 1, None, 0, 0),
    ("assignment_id", "TEXT", 1, None, 0, 0),
    ("message_id", "TEXT", 1, None, 0, 0),
    ("idempotency_key", "TEXT", 1, None, 0, 0),
    ("sender", "TEXT", 1, None, 0, 0),
    ("recipient", "TEXT", 1, None, 0, 0),
    ("role", "TEXT", 1, None, 0, 0),
    ("payload_digest", "BLOB", 1, None, 0, 0),
    ("sequence", "INTEGER", 1, None, 0, 0),
    ("state", "TEXT", 1, None, 0, 0),
    ("recorded_at", "TEXT", 1, None, 0, 0),
)

_EXPECTED_INDEXES = {
    "uq_message": ("project_id", "task_id", "message_id"),
    "uq_idem": ("project_id", "task_id", "idempotency_key"),
}

_EXPECTED_OBJECTS = frozenset(
    {
        ("table", "reservations", "reservations"),
        ("table", "sqlite_sequence", "sqlite_sequence"),
        ("index", "uq_message", "reservations"),
        ("index", "uq_idem", "reservations"),
    }
)

MIGRATION_REQUIRED = "migration required"
MALFORMED_PAYLOAD = "malformed payload"


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def _schema_problem(connection: sqlite3.Connection) -> str | None:
    """Return a description of the first structural mismatch, or None if exact v2.

    Uses only SELECT and PRAGMA reads. Indexes are matched by definition
    (uniqueness, origin, partial flag, key columns, collation, order), not by name only.
    """
    objects = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
    ).fetchall()
    found = frozenset((row[0], row[1], row[2]) for row in objects)
    if found != _EXPECTED_OBJECTS or len(objects) != len(_EXPECTED_OBJECTS):
        return "unexpected schema objects"
    table_sql = next(row[3] for row in objects if row[0] == "table" and row[1] == "reservations")
    if not isinstance(table_sql, str) or _normalize_sql(table_sql) != _normalize_sql(_TABLE_DDL):
        return "reservations table definition differs"
    if not re.search(r"\bid\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", table_sql, re.I):
        return "reservations.id lacks AUTOINCREMENT"
    columns = tuple(
        tuple(row[1:]) for row in connection.execute("PRAGMA table_xinfo(reservations)")
    )
    if columns != _EXPECTED_COLUMNS:
        return "reservations columns differ"
    index_list = connection.execute("PRAGMA index_list(reservations)").fetchall()
    if len(index_list) != len(_EXPECTED_INDEXES):
        return "unexpected index count"
    for _seq, name, unique, origin, partial in index_list:
        expected_keys = _EXPECTED_INDEXES.get(name)
        if expected_keys is None:
            return f"unexpected index {name}"
        if unique != 1 or origin != "c" or partial != 0:
            return f"index {name} is not a plain unique index"
        xinfo = connection.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
        keys = [row for row in xinfo if row[5] == 1]
        aux = [row for row in xinfo if row[5] != 1]
        if tuple(row[2] for row in keys) != expected_keys:
            return f"index {name} key columns differ"
        for _seqno, cid, _col, desc, coll, _key in keys:
            if cid < 0 or desc != 0 or coll != "BINARY":
                return f"index {name} key definition differs"
        if len(aux) != 1 or aux[0][1] != -1:
            return f"index {name} auxiliary columns differ"
    version = connection.execute("PRAGMA user_version").fetchone()
    if version is None or version[0] != SCHEMA_VERSION:
        return "user_version is not 2"
    return None


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


def _rejected(reason: str) -> ReservationResult:
    return ReservationResult("rejected", reason)


def _snapshot(envelope: object) -> SignedEnvelope | None:
    """Copy the payload once through the verifier's canonical encoder.

    Returns None on any failure; the caller rejects with 'malformed payload'.
    The copy is authenticated afterwards by verify_signature; this function
    proves nothing about authenticity on its own.
    """
    if type(envelope) is not SignedEnvelope:
        return None
    try:
        payload_bytes = _canonical_json(envelope.payload)
        snapshot = json.loads(payload_bytes)
        if type(snapshot) is not dict:
            return None
        return dataclasses.replace(envelope, payload=snapshot)
    except Exception:  # noqa: BLE001 - every copy failure is a controlled rejection
        return None


def _sha_problem(payload: dict, required_field: str) -> str | None:
    if ADAPTER_SHA_ALIAS in payload:
        return "adapter sha alias not accepted"
    for field_name in SHA_FIELDS:
        if field_name != required_field and field_name in payload:
            return f"forbidden field: {field_name}"
    value = payload.get(required_field)
    if type(value) is not str or _SHA_PATTERN.fullmatch(value) is None:
        return f"invalid {required_field}"
    return None


def _evidence_id_problem(payload: dict) -> str | None:
    value = payload.get("evidence_id")
    if type(value) is not str or not value.strip():
        return "missing or empty evidence_id"
    if value != value.strip():
        return "evidence_id has surrounding whitespace"
    return None


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
        fresh = self._preflight_read_only()  # I1
        self._before_locked_init()  # test seam between I1 and I2; no-op in production
        self._init_db(fresh)  # I2-I4
        self._block_if_unresolved()  # I5

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _preflight_read_only(self) -> bool:
        """I1: read-only structural preflight. Returns True when the store is fresh.

        Missing or 0-byte file: fresh. Otherwise the file is opened with mode=ro and
        only SELECT/PRAGMA reads run. Any mismatch fails closed with 'migration required'.
        A read-only open failure raises AdmissionError; there is no read-write fallback.
        """
        try:
            stat = self.database_path.stat()
        except FileNotFoundError:
            return True
        except OSError as exc:
            raise AdmissionError(f"storage failure during preflight: {exc}") from exc
        if not self.database_path.is_file():
            raise AdmissionError("storage failure during preflight: not a regular file")
        if stat.st_size == 0:
            return True
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                problem = _schema_problem(connection)
        except sqlite3.Error as exc:
            raise AdmissionError(f"storage failure during preflight: {exc}") from exc
        if problem is not None:
            raise AdmissionError(f"{MIGRATION_REQUIRED}: {problem}")
        return False

    def _before_locked_init(self) -> None:
        """Test seam between I1 (read-only preflight) and I2 (locked initialization)."""

    def _init_db(self, fresh: bool) -> None:
        """I2-I4: locked initialization and revalidation, then the WAL switch.

        I2  writable connection without WAL/synchronous PRAGMAs; BEGIN IMMEDIATE.
        I3  recheck under the lock: empty and fresh -> DDL + user_version, commit;
            otherwise full structural revalidation; incompatible -> rollback,
            'migration required'; compatible -> rollback the empty transaction.
        I4  outside any transaction: journal_mode=WAL (must report 'wal'),
            synchronous=FULL.
        I3 and I4 are not atomic together; see docs/bridge-02b2-admission-design.md.
        """
        connection = None
        try:
            connection = sqlite3.connect(self.database_path)  # I2
            connection.execute("BEGIN IMMEDIATE")
            object_count = connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if fresh and object_count == 0 and user_version == 0:  # I3 fresh
                connection.execute(_TABLE_DDL)
                for statement in _INDEX_DDL:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            else:  # I3 existing
                problem = _schema_problem(connection)
                connection.rollback()
                if problem is not None:
                    raise AdmissionError(f"{MIGRATION_REQUIRED}: {problem}")
            # I4: exactly one attempt. Contention (locked/busy) raises and fails closed
            # below; a concurrent initializer may therefore fail with AdmissionError and
            # the caller may retry construction.
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise AdmissionError(
                    f"storage failure during initialization: journal_mode is "
                    f"{None if mode is None else mode[0]!r}, not 'wal'"
                )
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise AdmissionError(f"storage failure during initialization: {exc}") from exc
        except AdmissionError:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if connection is not None:
                connection.close()

    def _block_if_unresolved(self) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT state FROM reservations WHERE state IN ('PENDING', 'UNCERTAIN') LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise AdmissionError(f"storage failure: {exc}") from exc
        if row:
            raise AdmissionError(f"Unresolved {row[0]} reservation blocks admission")

    def reserve(self, envelope: SignedEnvelope, now: datetime) -> ReservationResult:
        # 1. Initial database check (open, read, close).
        self._block_if_unresolved()

        # 2. HALT probe (unchanged; finding 6 probe-to-commit race remains open).
        try:
            halt = self.halt_probe.read()
        except Exception:
            return _rejected("HALT probe failed closed")
        if halt != "clear":
            return _rejected(f"HALT state {halt}")

        # Steps 3-9 are stateless: no further connection and no reservation write.
        # 3. One copied payload snapshot.
        snap = _snapshot(envelope)
        if snap is None:
            return _rejected(MALFORMED_PAYLOAD)

        # 4. The real verifier authenticates the copy. Everything below reads only `snap`.
        try:
            verdict = verify_signature(snap, self.trust_root, now)
        except SignatureError as exc:
            return _rejected(f"invalid signature: {exc}")
        except Exception as exc:  # noqa: BLE001 - fail closed
            return _rejected(f"invalid signature: {type(exc).__name__}")
        if verdict != "cryptographically_valid":
            return _rejected("invalid signature: not cryptographically valid")

        payload = snap.payload

        # 5. Message type.
        msg_type = payload.get("type")
        if type(msg_type) is not str or msg_type not in ALLOWED_TYPES:
            return _rejected("unauthorized message type")

        # 6. Closed authorization matrix (per-message actor binding).
        row = AUTHORIZATION_MATRIX.get((snap.sender, snap.role, snap.recipient, msg_type))
        if row is None:
            return _rejected("unauthorized sender/role/recipient/type combination")
        evidence_kind, sha_field = row

        # 7. evidence_id: strict string, no coercion, no trimming.
        problem = _evidence_id_problem(payload)
        if problem is not None:
            return _rejected(problem)
        evidence_id = payload["evidence_id"]

        # 8. Explicit SHA field for this type.
        problem = _sha_problem(payload, sha_field)
        if problem is not None:
            return _rejected(problem)
        bound_sha = payload[sha_field]

        # 9. Injected evidence verifier receives only str scalars.
        try:
            evidence = self.evidence_verifier.verify(
                evidence_kind,
                snap.assignment_id,
                bound_sha,
                evidence_id,
                now,
            )
        except Exception:
            return _rejected("evidence verification failed closed")
        if evidence != "verified":
            return _rejected(f"evidence {evidence}")

        # 10. Locked identity, sequence and assignment checks, then insert.
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT project_id, task_id, checkpoint_id, assignment_id,
                       message_id, idempotency_key, sender, recipient, role,
                       payload_digest, sequence, state
                FROM reservations
                WHERE project_id = ? AND task_id = ?
                  AND (message_id = ? OR idempotency_key = ?)
                """,
                (
                    snap.project_id,
                    snap.task_id,
                    snap.message_id,
                    snap.idempotency_key,
                ),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    connection.rollback()
                    return _rejected("ambiguous or conflicting identity matches")
                (
                    proj, task, ckpt, assign,
                    mid, idem, sender, recipient, role,
                    digest, seq, state,
                ) = rows[0]
                if (
                    proj == snap.project_id
                    and task == snap.task_id
                    and ckpt == snap.checkpoint_id
                    and assign == snap.assignment_id
                    and mid == snap.message_id
                    and idem == snap.idempotency_key
                    and sender == snap.sender
                    and recipient == snap.recipient
                    and role == snap.role
                    and digest == snap.payload_digest
                    and seq == snap.sequence
                    and state == "RESERVED"
                ):
                    connection.rollback()
                    return ReservationResult("duplicate", "identical reservation")
                connection.rollback()
                return _rejected("conflicting identity")

            last = connection.execute(
                """
                SELECT sequence, assignment_id FROM reservations
                WHERE project_id = ? AND task_id = ? AND checkpoint_id = ?
                  AND state = 'RESERVED'
                ORDER BY sequence DESC LIMIT 1
                """,
                (snap.project_id, snap.task_id, snap.checkpoint_id),
            ).fetchone()
            expected = 1 if last is None else last[0] + 1
            if snap.sequence != expected:
                connection.rollback()
                return _rejected(f"sequence {snap.sequence} != {expected}")
            if last is not None and last[1] != snap.assignment_id:
                connection.rollback()
                return _rejected("assignment binding mismatch")

            connection.execute(
                """
                INSERT INTO reservations (
                    project_id, task_id, checkpoint_id, assignment_id,
                    message_id, idempotency_key, sender, recipient, role,
                    payload_digest, sequence, state, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?)
                """,
                (
                    snap.project_id,
                    snap.task_id,
                    snap.checkpoint_id,
                    snap.assignment_id,
                    snap.message_id,
                    snap.idempotency_key,
                    snap.sender,
                    snap.recipient,
                    snap.role,
                    snap.payload_digest,
                    snap.sequence,
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
