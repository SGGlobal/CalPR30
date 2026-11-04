"""
SKIPPY AI TRADER — PHASE 8

Persistent Replay Registry.

LOCAL SIMULATION ONLY.
No network access or trade execution.
"""

import sqlite3
from pathlib import Path


class ReplayRegistryError(RuntimeError):
    """Raised when replay protection cannot be guaranteed."""


class PersistentReplayRegistry:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

        if str(database_path) == ":memory:":
            raise ValueError("Persistent registry requires a disk database")

        if not self.database_path.parent.is_dir():
            raise ReplayRegistryError("Database directory does not exist")

        try:
            with self._connect() as connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        fingerprint TEXT PRIMARY KEY,
                        first_seen TEXT NOT NULL
                    )
                """)
        except sqlite3.Error as exc:
            raise ReplayRegistryError("Failed to initialize registry") from exc

    def _connect(self):
        return sqlite3.connect(
            self.database_path,
            timeout=5.0,
        )

    def register(self, fingerprint: str, first_seen: str) -> bool:
        """
        Atomically register a fingerprint.

        Returns True for a new signal.
        Returns False for a previously registered signal.

        Database failures raise ReplayRegistryError.
        """

        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("Invalid SHA-256 fingerprint")

        if not isinstance(first_seen, str) or not first_seen:
            raise ValueError("Invalid first_seen timestamp")

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO signals (
                        fingerprint,
                        first_seen
                    )
                    VALUES (?, ?)
                    """,
                    (fingerprint, first_seen),
                )

                return cursor.rowcount == 1

        except sqlite3.Error as exc:
            raise ReplayRegistryError(
                "Replay registry unavailable; signal must be rejected"
            ) from exc