"""
SKIPPY AI TRADER - PHASE 8

Centralized Observational Database Initializer.

LOCAL SIMULATION ONLY.
No networking, retries, or trade execution.
"""

import sqlite3
from pathlib import Path

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.observation_repository import (
    ObservationRepository,
)


class DatabaseInitializationError(RuntimeError):
    """Raised when database initialization cannot be verified."""


class TradingViewDatabaseInitializer:
    REQUIRED_COLUMNS = {
        "signals": {
            "fingerprint",
            "first_seen",
            "evidence_json",
        },
        "processing_states": {
            "fingerprint",
            "state",
            "updated_at",
        },
        "observations": {
            "fingerprint",
            "observed_at",
            "result_json",
        },
    }

    def __init__(self, database_path: str | Path):
        if (
            database_path is None
            or str(database_path) in {"", ":memory:"}
        ):
            raise ValueError(
                "Initialization requires a persistent database path"
            )

        self.database_path = Path(database_path)

        if not self.database_path.parent.is_dir():
            raise DatabaseInitializationError(
                "Database directory does not exist"
            )

    def initialize(self) -> None:
        """
        Initialize and verify the observational database.

        Existing signal and observation records are preserved.
        """

        try:
            AtomicSignalRegistry(self.database_path)

            ObservationRepository(self.database_path)

            self.verify()

        except (sqlite3.Error, RuntimeError, ValueError) as exc:
            raise DatabaseInitializationError(
                "Observational database initialization failed"
            ) from exc

    def verify(self) -> None:
        """
        Verify required tables and columns.

        Verification is read-only.
        """

        if not self.database_path.is_file():
            raise DatabaseInitializationError(
                "Database does not exist"
            )

        try:
            uri = self.database_path.resolve().as_uri() + "?mode=ro"

            with sqlite3.connect(uri, uri=True) as connection:
                connection.execute("PRAGMA query_only = ON")

                for table, required_columns in self.REQUIRED_COLUMNS.items():
                    columns = {
                        row[1]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }

                    if not required_columns.issubset(columns):
                        raise DatabaseInitializationError(
                            f"Invalid observational schema: {table}"
                        )

        except sqlite3.Error as exc:
            raise DatabaseInitializationError(
                "Unable to verify observational database"
            ) from exc
