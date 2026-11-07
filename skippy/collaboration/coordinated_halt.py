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
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

SUPPORTED_SCHEMA_VERSION = "1"
MAX_REASON_LENGTH = 128
LOCK_TIMEOUT_SECONDS = 5.0


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
                return status, sequence
            finally:
                connection.close()
        except Exception:
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
        """

        if not self.database_path.is_file():
            raise HaltError("Authority database missing")

        normalized = self._normalize_reason(reason)

        connection = self._connect(readonly=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            status, sequence = self._read_validated(connection)
            if status is HaltStatus.UNKNOWN or sequence is None:
                connection.rollback()
                raise HaltError("Authority state cannot be trusted")
            if status is HaltStatus.HALTED:
                connection.commit()
                committed_sequence = sequence
                duplicate = True
            elif status is HaltStatus.NOT_HALTED:
                committed_sequence = sequence + 1
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE halt_state
                    SET state = ?, sequence = ?, updated_at = ?, reason = ?
                    WHERE singleton = 1 AND state = ?
                    """,
                    (
                        HaltStatus.HALTED.value,
                        committed_sequence,
                        now,
                        normalized,
                        HaltStatus.NOT_HALTED.value,
                    ),
                )
                if connection.total_changes != 1:
                    connection.rollback()
                    raise HaltError("HALT transition did not apply")
                connection.commit()
                duplicate = False
            else:
                connection.rollback()
                raise HaltError("Unexpected HALT state")
        except HaltError:
            raise
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise HaltError("HALT activation failed") from exc
        finally:
            connection.close()

        self._audit_best_effort(
            "duplicate_halt_request" if duplicate else "halt_activated",
            committed_sequence,
            normalized,
        )
        return committed_sequence

    def _connect(self, readonly: bool) -> sqlite3.Connection:
        """
        Open the existing authority database.

        Uses SQLite URI mode=rw or mode=ro so a missing file is never created.
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
        if state not in (HaltStatus.NOT_HALTED.value, HaltStatus.HALTED.value):
            return HaltStatus.UNKNOWN, None
        if not isinstance(sequence, int) or sequence < 0:
            return HaltStatus.UNKNOWN, None
        return HaltStatus(state), sequence

    def _normalize_reason(self, reason: str) -> str:
        if not isinstance(reason, str):
            return "unspecified"
        cleaned = "".join(ch for ch in reason if ch.isprintable())
        cleaned = cleaned.strip()
        if not cleaned:
            return "unspecified"
        if len(cleaned) > MAX_REASON_LENGTH:
            cleaned = cleaned[:MAX_REASON_LENGTH]
        return cleaned

    def _audit_best_effort(
        self, event_type: str, sequence: int | None, detail: str
    ) -> None:
        """
        Best-effort secondary audit. Never raises. Never rolls back HALT.
        """

        try:
            if not self.database_path.is_file():
                return
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
