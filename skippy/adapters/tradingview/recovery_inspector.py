"""
SKIPPY AI TRADER - PHASE 8

Read-Only TradingView Recovery Inspector.

LOCAL SIMULATION ONLY.
No database mutations, retries, or trade execution.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class RecoveryInspectionError(RuntimeError):
    """Raised when recovery inspection cannot be completed safely."""


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
