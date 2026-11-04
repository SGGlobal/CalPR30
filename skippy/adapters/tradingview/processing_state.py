"""
SKIPPY AI TRADER — PHASE 8

Persistent Observational Processing-State Registry.

LOCAL SIMULATION ONLY.
No automated retries or trade execution.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ProcessingStateError(RuntimeError):
    """Raised when processing-state integrity cannot be guaranteed."""


class ProcessingStateRegistry:
    ALLOWED_TRANSITIONS = {
        "RECEIVED": {"PROCESSING", "REVIEW_REQUIRED"},
        "PROCESSING": {"COMPLETED", "FAILED", "REVIEW_REQUIRED"},
        "FAILED": {"REVIEW_REQUIRED"},
        "COMPLETED": set(),
        "REVIEW_REQUIRED": set(),
    }

    def __init__(self, database_path: str | Path):
        if str(database_path) == ":memory:":
            raise ValueError("Persistent state registry requires a disk database")

        self.database_path = Path(database_path)

        if not self.database_path.parent.is_dir():
            raise ProcessingStateError("Database directory does not exist")

        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processing_states (
                        fingerprint TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as exc:
            raise ProcessingStateError("Failed to initialize state registry") from exc

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    @staticmethod
    def _timestamp():
        return datetime.now(timezone.utc).isoformat()

    def register(self, fingerprint: str) -> bool:
        """Create a RECEIVED record without overwriting existing state."""

        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("Invalid fingerprint")

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO processing_states
                    (fingerprint, state, updated_at)
                    VALUES (?, 'RECEIVED', ?)
                    """,
                    (fingerprint, self._timestamp()),
                )

                return cursor.rowcount == 1

        except sqlite3.Error as exc:
            raise ProcessingStateError("State registration failed") from exc

    def get_state(self, fingerprint: str) -> str | None:
        """Return the persisted state for a fingerprint."""

        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT state
                    FROM processing_states
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()

                return row[0] if row else None

        except sqlite3.Error as exc:
            raise ProcessingStateError("State lookup failed") from exc

    def transition(self, fingerprint: str, new_state: str) -> None:
        """Atomically apply a permitted state transition."""

        if new_state not in self.ALLOWED_TRANSITIONS:
            raise ProcessingStateError("Unknown processing state")

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")

                row = connection.execute(
                    """
                    SELECT state
                    FROM processing_states
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()

                if row is None:
                    raise ProcessingStateError("Signal is not registered")

                current_state = row[0]

                if new_state not in self.ALLOWED_TRANSITIONS[current_state]:
                    raise ProcessingStateError(
                        f"Invalid transition: {current_state} -> {new_state}"
                    )

                connection.execute(
                    """
                    UPDATE processing_states
                    SET state = ?, updated_at = ?
                    WHERE fingerprint = ?
                    """,
                    (new_state, self._timestamp(), fingerprint),
                )

        except sqlite3.Error as exc:
            raise ProcessingStateError("State transition failed") from exc
