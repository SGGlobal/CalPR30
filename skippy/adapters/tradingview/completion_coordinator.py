"""
SKIPPY AI TRADER - PHASE 8

Transactional Observation Completion Coordinator.

LOCAL SIMULATION ONLY.
No external connections, retries, or trade execution.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.observation_repository import ObservationRepository


class CompletionError(RuntimeError):
    """Raised when transactional completion cannot be guaranteed."""


class ObservationCompletionCoordinator:
    def __init__(self, database_path: str | Path):
        if database_path is None:
            raise ValueError("Completion requires persistent storage")

        self.database_path = Path(database_path)

        # Initialize the existing schemas in the same database.
        AtomicSignalRegistry(self.database_path)
        ObservationRepository(self.database_path)

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    def complete(self, fingerprint: str, result: dict) -> None:
        """
        Save an observation and transition PROCESSING -> COMPLETED.

        Both changes must succeed or both must roll back.
        """

        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("Invalid SHA-256 fingerprint")

        if not isinstance(result, dict) or not result:
            raise ValueError("Observation must be a nonempty dictionary")

        try:
            serialized = json.dumps(
                result,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Observation is not valid JSON data") from exc

        timestamp = datetime.now(timezone.utc).isoformat()

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
                    raise CompletionError("Signal is not registered")

                if row[0] != "PROCESSING":
                    raise CompletionError(
                        f"Cannot complete signal in state {row[0]}"
                    )

                replay = connection.execute(
                    """
                    SELECT 1 FROM signals
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()

                if replay is None:
                    raise CompletionError(
                        "Missing replay record; completion refused"
                    )

                connection.execute(
                    """
                    INSERT INTO observations (
                        fingerprint,
                        observed_at,
                        result_json
                    )
                    VALUES (?, ?, ?)
                    """,
                    (fingerprint, timestamp, serialized),
                )

                cursor = connection.execute(
                    """
                    UPDATE processing_states
                    SET state = 'COMPLETED',
                        updated_at = ?
                    WHERE fingerprint = ?
                      AND state = 'PROCESSING'
                    """,
                    (timestamp, fingerprint),
                )

                if cursor.rowcount != 1:
                    raise CompletionError(
                        "Processing state could not be completed"
                    )

        except sqlite3.Error as exc:
            raise CompletionError(
                "Transactional observation completion failed"
            ) from exc