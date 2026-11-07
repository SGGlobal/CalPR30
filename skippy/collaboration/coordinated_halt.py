"""
SKIPPY AI Collaboration Bridge — Isolated Coordinated HALT Authority.

OFFLINE. SYNTHETIC. NON-EXECUTING.

This module is the isolated HALT authority. It is not integrated into
AuthorizationPolicy, CollaborationProtocol, AuthorizationLifecycle, or
ChiefIdentityVerifier in this checkpoint.

Normal construction and operation never create the authority database.
There is no production bootstrap, no HALT clearing, and no automatic recovery.
execution_enabled remains False. No method grants protected execution.

Durability: the mandatory halt_state transaction uses BEGIN IMMEDIATE and
commits before success is reported. synchronous=FULL is requested on write
connections. This is not tamper-proof storage. A crash after commit is
durable under normal SQLite FULL sync; power loss or filesystem bugs can
still lose a commit. Secondary audit writes are best-effort and never
roll back a committed HALT.

A successful read of NOT_HALTED is only a point-in-time observation.
Separate databases retain independent transaction boundaries.

Audit events currently supported (best-effort, secondary):
- halt_requested
- halt_activated
- duplicate_halt_request
- halt_read_failure
- coordination_failure

An unavailable authority cannot always persist its own read-failure event.
The mandatory HALT state never depends on secondary audit persistence.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

SUPPORTED_SCHEMA_VERSION = "1"
LOCK_TIMEOUT_SECONDS = 5.0

ALLOWED_REASON_CODES = frozenset(
    {
        "operator",
        "policy",
        "protocol",
        "migration",
        "coordination",
        "test",
        "unspecified",
    }
)


class HaltStatus(str, Enum):
    NOT_HALTED = "NOT_HALTED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


class HaltError(RuntimeError):
    """Raised when HALT state cannot be established as clear or activation fails."""


class CoordinatedHalt:
    """
    Isolated coordinated HALT authority.

    Opens an existing SQLite control database. Never creates it.
    Never clears HALT. Never grants execution permission.
    """

    execution_enabled = False

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def read_status(self) -> tuple[HaltStatus, int | None]:
        """
        Return the current authoritative HALT state.

        Never interprets a missing record as NOT_HALTED.
        Returns UNKNOWN when persistence cannot be trusted.
        Never raises for ordinary read failures.
        Never creates the database.
        """

        try:
            if not self.database_path.is_file():
                return HaltStatus.UNKNOWN, None
            connection = self._connect(readonly=True)
            try:
                connection.execute("BEGIN")
                status, sequence = self._read_validated(connection)
                connection.rollback()
            finally:
                connection.close()
            if status is HaltStatus.UNKNOWN:
                self._audit_best_effort("halt_read_failure", None, "unspecified")
            return status, sequence
        except Exception:
            self._audit_best_effort("halt_read_failure", None, "unspecified")
            return HaltStatus.UNKNOWN, None

    def require_not_halted(self) -> None:
        """
        Permit continued offline evaluation only when state is exactly NOT_HALTED.

        Raises HaltError for HALTED, UNKNOWN, missing, corrupt, lock timeout,
        or invalid schema. Does not grant protected execution permission.
        """

        status, _sequence = self.read_status()
        if status is not HaltStatus.NOT_HALTED:
            raise HaltError(f"HALT state is {status.value}")

    def activate_halt(self, reason: str) -> int:
        """
        Durably activate HALT.

        Returns the sequence number after the mandatory transaction commits.
        Idempotent if already HALTED (sequence is not incremented).
        Raises HaltError if the write cannot be committed.
        Never creates a missing authority. Never clears HALT.
        Never grants execution permission.
        Public API raises only HaltError for expected persistence failures.
        """

        if not self.database_path.is_file():
            raise HaltError("Authority database missing")

        code = self._reason_code(reason)
        self._audit_best_effort("halt_requested", None, code)

        connection = None
        try:
            connection = self._connect(readonly=False)
            connection.execute("BEGIN IMMEDIATE")
            status, sequence = self._read_validated(connection)
            if status is HaltStatus.UNKNOWN or sequence is None:
                self._rollback(connection)
                raise HaltError("Authority state cannot be trusted")
            if status is HaltStatus.HALTED:
                connection.commit()
                committed_sequence = sequence
                duplicate = True
            elif status is HaltStatus.NOT_HALTED:
                committed_sequence = sequence + 1
                now = datetime.now(timezone.utc).isoformat()
                cursor = connection.execute(
                    """
                    UPDATE halt_state
                    SET state = ?, sequence = ?, updated_at = ?, reason = ?
                    WHERE singleton = 1 AND state = ? AND sequence = 0
                    """,
                    (
                        HaltStatus.HALTED.value,
                        committed_sequence,
                        now,
                        code,
                        HaltStatus.NOT_HALTED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    self._rollback(connection)
                    raise HaltError("HALT transition did not apply")
                connection.commit()
                duplicate = False
            else:
                self._rollback(connection)
                raise HaltError("Unexpected HALT state")
        except HaltError:
            raise
        except Exception as exc:
            self._rollback(connection)
            self._audit_best_effort("coordination_failure", None, "unspecified")
            raise HaltError("HALT activation failed") from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        self._audit_best_effort(
            "duplicate_halt_request" if duplicate else "halt_activated",
            committed_sequence,
            code,
        )
        return committed_sequence

    def _connect(self, readonly: bool) -> sqlite3.Connection:
        """
        Open the existing authority database.

        Uses SQLite URI mode=rw or mode=ro so a missing file is never created.
        Configuration failures propagate to the caller, which maps them to
        HaltError on the public activation path.
        """

        if not self.database_path.is_file():
            raise HaltError("Authority database missing")
        mode = "ro" if readonly else "rw"
        uri = self.database_path.resolve().as_uri() + f"?mode={mode}"
        connection = sqlite3.connect(
            uri, uri=True, timeout=LOCK_TIMEOUT_SECONDS
        )
        if not readonly:
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _read_validated(
        self, connection: sqlite3.Connection
    ) -> tuple[HaltStatus, int | None]:
        version_row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if version_row is None or version_row[0] != SUPPORTED_SCHEMA_VERSION:
            return HaltStatus.UNKNOWN, None

        row = connection.execute(
            """
            SELECT state, sequence
            FROM halt_state
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            return HaltStatus.UNKNOWN, None

        state, sequence = row
        if not isinstance(sequence, int):
            return HaltStatus.UNKNOWN, None
        if state == HaltStatus.NOT_HALTED.value:
            if sequence != 0:
                return HaltStatus.UNKNOWN, None
            return HaltStatus.NOT_HALTED, sequence
        if state == HaltStatus.HALTED.value:
            if sequence < 1:
                return HaltStatus.UNKNOWN, None
            return HaltStatus.HALTED, sequence
        return HaltStatus.UNKNOWN, None

    def _reason_code(self, reason: str) -> str:
        """
        Map caller input to a controlled reason code.

        Arbitrary free-form text, including credential-bearing strings, is
        never persisted. Unknown or malformed input becomes 'unspecified'.
        """

        if not isinstance(reason, str):
            return "unspecified"
        candidate = reason.strip()
        if candidate in ALLOWED_REASON_CODES:
            return candidate
        return "unspecified"

    def _audit_best_effort(
        self, event_type: str, sequence: int | None, detail: str
    ) -> None:
        """
        Best-effort secondary audit. Never raises. Never rolls back HALT.

        detail must already be a controlled reason code. If the authority
        cannot be opened, the event is dropped. An unavailable authority
        cannot be assumed to persist its own failure event.
        """

        try:
            if not self.database_path.is_file():
                return
            if detail not in ALLOWED_REASON_CODES:
                detail = "unspecified"
            connection = self._connect(readonly=False)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO halt_events (
                        event_id, occurred_at, event_type, sequence, detail
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        datetime.now(timezone.utc).isoformat(),
                        event_type,
                        sequence,
                        detail,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        except Exception:
            return

    @staticmethod
    def _rollback(connection: sqlite3.Connection | None) -> None:
        if connection is None:
            return
        try:
            connection.rollback()
        except Exception:
            return
