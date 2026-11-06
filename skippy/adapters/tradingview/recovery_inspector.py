"""
SKIPPY AI TRADER - PHASE 8

Read-Only TradingView Recovery Inspector.

LOCAL SIMULATION ONLY.
No database mutations, retries, or trade execution.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skippy.adapters.tradingview.deduplicator import SignalDeduplicator
from skippy.adapters.tradingview.validator import validate_signal


class RecoveryInspectionError(RuntimeError):
    """Raised when recovery inspection cannot be completed safely."""


ALLOWED_EVIDENCE_KEYS = frozenset({"symbol", "action", "price", "timestamp"})


class RecoveryInspector:
    VALID_STATES = {
        "RECEIVED",
        "PROCESSING",
        "COMPLETED",
        "FAILED",
        "REVIEW_REQUIRED",
    }

    def __init__(
        self,
        database_path: str | Path,
        stale_after_seconds: int = 300,
    ):
        if database_path is None or str(database_path) == ":memory:":
            raise ValueError("Inspection requires an existing disk database")

        if (
            isinstance(stale_after_seconds, bool)
            or not isinstance(stale_after_seconds, int)
            or stale_after_seconds <= 0
        ):
            raise ValueError("Stale threshold must be a positive integer")

        self.database_path = Path(database_path)
        self.stale_after_seconds = stale_after_seconds

        if not self.database_path.is_file():
            raise RecoveryInspectionError("Database does not exist")

    def _connect(self):
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0)

    @staticmethod
    def _parse_timestamp(value):
        if not isinstance(value, str):
            raise ValueError("Timestamp must be text")

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if parsed.tzinfo is None:
            raise ValueError("Timestamp must contain timezone information")

        return parsed.astimezone(timezone.utc)

    def inspect(self, now: datetime | None = None) -> list[dict]:
        """
        Inspect replay, processing-state, and observation records.

        No database modifications or automatic retries.
        Evidence is intentionally omitted from the default report.
        """

        if now is None:
            now = datetime.now(timezone.utc)

        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Inspection time must be timezone-aware")

        now = now.astimezone(timezone.utc)

        stale_before = now - timedelta(
            seconds=self.stale_after_seconds
        )

        try:
            with self._connect() as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")

                rows = connection.execute(
                    """
                    SELECT
                        s.fingerprint,
                        s.first_seen,
                        p.state,
                        p.updated_at,
                        CASE WHEN o.fingerprint IS NULL THEN 0 ELSE 1 END
                    FROM signals AS s
                    LEFT JOIN processing_states AS p
                        ON p.fingerprint = s.fingerprint
                    LEFT JOIN observations AS o
                        ON o.fingerprint = s.fingerprint

                    UNION ALL

                    SELECT
                        p.fingerprint,
                        NULL,
                        p.state,
                        p.updated_at,
                        CASE WHEN o.fingerprint IS NULL THEN 0 ELSE 1 END
                    FROM processing_states AS p
                    LEFT JOIN signals AS s
                        ON s.fingerprint = p.fingerprint
                    LEFT JOIN observations AS o
                        ON o.fingerprint = p.fingerprint
                    WHERE s.fingerprint IS NULL

                    UNION ALL

                    SELECT
                        o.fingerprint,
                        NULL,
                        NULL,
                        NULL,
                        1
                    FROM observations AS o
                    LEFT JOIN signals AS s
                        ON s.fingerprint = o.fingerprint
                    LEFT JOIN processing_states AS p
                        ON p.fingerprint = o.fingerprint
                    WHERE s.fingerprint IS NULL
                      AND p.fingerprint IS NULL

                    ORDER BY 1
                    """
                ).fetchall()

        except sqlite3.Error as exc:
            raise RecoveryInspectionError(
                "Unable to inspect persistent signal records"
            ) from exc

        report = []

        for fingerprint, first_seen, state, updated_at, has_observation in rows:
            classification = "INCONSISTENT"
            reason = "Database records disagree"

            if first_seen is not None and state in self.VALID_STATES:
                try:
                    first_seen_time = self._parse_timestamp(first_seen)
                    updated_time = self._parse_timestamp(updated_at)

                    if updated_time < first_seen_time or updated_time > now:
                        reason = "Processing timestamps are inconsistent"

                    elif state == "COMPLETED" and has_observation:
                        classification = "COMPLETED"
                        reason = "Observation stored and processing completed"

                    elif state == "COMPLETED":
                        reason = "Completed signal has no observation"

                    elif has_observation:
                        reason = "Observation exists without completed state"

                    elif state in {"FAILED", "REVIEW_REQUIRED"}:
                        classification = "REVIEW_REQUIRED"
                        reason = f"Signal is in {state} state"

                    elif updated_time < stale_before:
                        classification = "STALE"
                        reason = "Signal exceeded unfinished age threshold"

                    else:
                        classification = "IN_PROGRESS"
                        reason = "Signal has not exceeded unfinished age threshold"

                except (ValueError, OverflowError):
                    reason = "Invalid processing timestamp"

            report.append({
                "fingerprint": fingerprint,
                "state": state,
                "classification": classification,
                "reason": reason,
            })

        return report

    def get_evidence(self, fingerprint: str) -> dict | None:
        """
        Retrieve stored signal evidence for a fingerprint.

        Returns None when the record has no evidence (historical NULL).
        Read-only; does not modify state.

        This method does not re-validate content or recompute the fingerprint.
        Use get_verified_evidence for integrity checking.
        """

        try:
            with self._connect() as connection:
                connection.execute("PRAGMA query_only = ON")

                row = connection.execute(
                    """
                    SELECT evidence_json
                    FROM signals
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()

        except sqlite3.Error as exc:
            raise RecoveryInspectionError(
                "Unable to retrieve signal evidence"
            ) from exc

        if row is None or row[0] is None:
            return None

        try:
            parsed = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise RecoveryInspectionError(
                "Stored evidence contains invalid JSON"
            ) from exc

        if not isinstance(parsed, dict):
            raise RecoveryInspectionError("Stored evidence is not an object")

        return parsed

    def get_verified_evidence(self, fingerprint: str) -> dict | None:
        """
        Retrieve and integrity-check stored signal evidence.

        Returns None for historical records with NULL evidence.
        When evidence is present it is validated and its fingerprint is
        recomputed. Mismatches and invalid content raise
        RecoveryInspectionError.

        Read-only. Does not modify processing state, re-publish, or
        trigger recovery.

        Note: fingerprint consistency detects mismatches between stored
        evidence and its fingerprint. It does not prove authenticity
        against an attacker capable of modifying both the evidence and
        the fingerprint together.
        """

        try:
            with self._connect() as connection:
                connection.execute("PRAGMA query_only = ON")

                row = connection.execute(
                    """
                    SELECT evidence_json
                    FROM signals
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()

        except sqlite3.Error as exc:
            raise RecoveryInspectionError(
                "Unable to retrieve signal evidence"
            ) from exc

        if row is None:
            raise RecoveryInspectionError("Signal fingerprint not found")

        raw = row[0]

        # Historical NULL evidence is supported.
        if raw is None:
            return None

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RecoveryInspectionError(
                "Stored evidence contains invalid JSON"
            ) from exc

        if not isinstance(parsed, dict):
            raise RecoveryInspectionError("Stored evidence is not an object")

        # Strict field set before validation.
        if set(parsed.keys()) != ALLOWED_EVIDENCE_KEYS:
            raise RecoveryInspectionError(
                "Stored evidence does not contain exactly the approved fields"
            )

        try:
            validated = validate_signal(parsed)
        except Exception as exc:
            raise RecoveryInspectionError(
                "Stored evidence failed signal validation"
            ) from exc

        recomputed = SignalDeduplicator().fingerprint(validated)

        if recomputed != fingerprint:
            raise RecoveryInspectionError(
                "Stored evidence fingerprint does not match record fingerprint"
            )

        # Return the canonical validated form.
        return {
            "symbol": validated.symbol,
            "action": validated.action,
            "price": validated.price,
            "timestamp": validated.timestamp,
        }
