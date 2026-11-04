"""
SKIPPY AI TRADER - PHASE 8

Atomic Replay and Processing-State Registration.

LOCAL SIMULATION ONLY.
No external connections, retries, or trade execution.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class AtomicRegistryError(RuntimeError):
    """Raised when atomic registration cannot be guaranteed."""


class AtomicSignalRegistry:
    def __init__(self, database_path: str | Path):
        if str(database_path) == ":memory:":
            raise ValueError("Atomic registry requires a disk database")

        self.database_path = Path(database_path)

        if not self.database_path.parent.is_dir():
            raise AtomicRegistryError("Database directory does not exist")

        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signals (
                        fingerprint TEXT PRIMARY KEY,
                        first_seen TEXT NOT NULL
                    )
                    """
                )

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
            raise AtomicRegistryError(
                "Failed to initialize atomic registry"
            ) from exc

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    @staticmethod
    def _timestamp():
        return datetime.now(timezone.utc).isoformat()

    def register(self, fingerprint: str) -> bool:
        """
        Atomically register replay protection and processing state.

        Returns True only for a new signal.
        Returns False for an existing signal.
        """

        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("Invalid SHA-256 fingerprint")

        timestamp = self._timestamp()

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO signals (
                        fingerprint,
                        first_seen
                    )
                    VALUES (?, ?)
                    """,
                    (fingerprint, timestamp),
                )

                if cursor.rowcount == 0:
                    return False

                connection.execute(
                    """
                    INSERT INTO processing_states (
                        fingerprint,
                        state,
                        updated_at
                    )
                    VALUES (?, 'RECEIVED', ?)
                    """,
                    (fingerprint, timestamp),
                )

                return True

        except sqlite3.Error as exc:
            raise AtomicRegistryError(
                "Atomic signal registration failed"
            ) from exc

    def get_state(self, fingerprint: str) -> str | None:
        """Return the stored processing state."""

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
            raise AtomicRegistryError(
                "Atomic registry state lookup failed"
            ) from exc
