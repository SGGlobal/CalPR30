"""
SKIPPY AI TRADER - PHASE 8

Atomic Replay and Processing-State Registration.

LOCAL SIMULATION ONLY.
No external connections, retries, or trade execution.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from skippy.adapters.tradingview.deduplicator import SignalDeduplicator
from skippy.adapters.tradingview.validator import validate_signal


class AtomicRegistryError(RuntimeError):
    """Raised when atomic registration cannot be guaranteed."""


ALLOWED_EVIDENCE_KEYS = frozenset({"symbol", "action", "price", "timestamp"})


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

                # Idempotent additive migration for durable evidence.
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(signals)"
                    ).fetchall()
                }

                if "evidence_json" not in columns:
                    connection.execute(
                        "ALTER TABLE signals ADD COLUMN evidence_json TEXT"
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

    @staticmethod
    def _normalize_evidence(evidence: dict | None) -> str | None:
        """
        Validate and canonicalize evidence when supplied.

        Returns a canonical JSON string or None.
        Rejects invalid content fail-closed before any database access.
        """

        if evidence is None:
            return None

        if not isinstance(evidence, dict):
            raise ValueError("Evidence must be a dictionary")

        # Strict field set: exactly the four approved keys.
        # Reject missing or additional keys before calling the validator.
        if set(evidence.keys()) != ALLOWED_EVIDENCE_KEYS:
            raise ValueError(
                "Evidence must contain exactly symbol, action, price, and timestamp"
            )

        # Reuse the existing signal validator.
        # This enforces allowed symbols/actions, numeric price,
        # and timezone-aware timestamp.
        validated = validate_signal(evidence)

        # Canonical representation used for fingerprinting and storage.
        canonical = {
            "symbol": validated.symbol,
            "action": validated.action,
            "price": validated.price,
            "timestamp": validated.timestamp,
        }

        try:
            serialized = json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Evidence is not valid JSON data") from exc

        return serialized

    def register(
        self,
        fingerprint: str,
        evidence: dict | None = None,
    ) -> bool:
        """
        Atomically register replay protection and processing state.

        Returns True only for a new signal.
        Returns False for an existing signal.

        When evidence is supplied it is validated, normalized, and
        required to match the supplied fingerprint before any mutation.
        Duplicate registrations do not modify existing evidence.
        Fingerprint-only registration remains supported for legacy paths.
        """

        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("Invalid SHA-256 fingerprint")

        evidence_json = self._normalize_evidence(evidence)

        # When evidence is present, recompute and enforce fingerprint match
        # before opening a database transaction.
        if evidence_json is not None:
            validated = validate_signal(evidence)
            recomputed = SignalDeduplicator().fingerprint(validated)

            if recomputed != fingerprint:
                raise ValueError(
                    "Evidence fingerprint does not match supplied fingerprint"
                )

        timestamp = self._timestamp()

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO signals (
                        fingerprint,
                        first_seen,
                        evidence_json
                    )
                    VALUES (?, ?, ?)
                    """,
                    (fingerprint, timestamp, evidence_json),
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

    def get_evidence(self, fingerprint: str) -> dict | None:
        """Return stored validated evidence or None."""

        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT evidence_json
                    FROM signals
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()

        except sqlite3.Error as exc:
            raise AtomicRegistryError(
                "Atomic registry evidence lookup failed"
            ) from exc

        if row is None or row[0] is None:
            return None

        try:
            parsed = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise AtomicRegistryError(
                "Stored evidence contains invalid JSON"
            ) from exc

        if not isinstance(parsed, dict):
            raise AtomicRegistryError("Stored evidence is not an object")

        return parsed
