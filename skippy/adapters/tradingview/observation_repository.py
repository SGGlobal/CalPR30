"""
SKIPPY AI TRADER - PHASE 8

Persistent Hermes Observation Repository.

LOCAL SIMULATION ONLY.
No broker connections or trade execution.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ObservationRepositoryError(RuntimeError):
    """Raised when persistent observation storage fails."""


class ObservationRepository:
    def __init__(self, database_path: str | Path):
        if str(database_path) == ":memory:":
            raise ValueError("Persistent observation storage requires a disk database")

        self.database_path = Path(database_path)

        if not self.database_path.parent.is_dir():
            raise ObservationRepositoryError("Database directory does not exist")

        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS observations (
                        fingerprint TEXT PRIMARY KEY,
                        observed_at TEXT NOT NULL,
                        result_json TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as exc:
            raise ObservationRepositoryError(
                "Failed to initialize observation repository"
            ) from exc

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    def save(self, fingerprint: str, result: dict) -> bool:
        """
        Persist a new observation without overwriting existing evidence.

        Returns True if saved.
        Returns False if the fingerprint already exists.
        """

        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("Invalid SHA-256 fingerprint")

        if not isinstance(result, dict) or not result:
            raise ValueError("Observation result must be a nonempty dictionary")

        try:
            serialized = json.dumps(
                result,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Observation result is not valid JSON data") from exc

        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO observations (
                        fingerprint,
                        observed_at,
                        result_json
                    )
                    VALUES (?, ?, ?)
                    """,
                    (fingerprint, timestamp, serialized),
                )

                return cursor.rowcount == 1

        except sqlite3.Error as exc:
            raise ObservationRepositoryError(
                "Failed to persist observation"
            ) from exc

    def get(self, fingerprint: str) -> dict | None:
        """Retrieve a previously saved observation."""

        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT observed_at, result_json
                    FROM observations
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()

        except sqlite3.Error as exc:
            raise ObservationRepositoryError(
                "Failed to retrieve observation"
            ) from exc

        if row is None:
            return None

        try:
            result = json.loads(row[1])
        except (TypeError, ValueError) as exc:
            raise ObservationRepositoryError(
                "Stored observation contains invalid JSON"
            ) from exc

        return {
            "fingerprint": fingerprint,
            "observed_at": row[0],
            "result": result,
        }