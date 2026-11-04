"""
SKIPPY AI TRADER - PHASE 8

Read-Only Manual Investigation Report.

LOCAL SIMULATION ONLY.
No recovery actions or trade execution.
"""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from skippy.adapters.tradingview.recovery_inspector import RecoveryInspector


class InvestigationReport:
    CLASSIFICATIONS = (
        "COMPLETED",
        "IN_PROGRESS",
        "STALE",
        "REVIEW_REQUIRED",
        "INCONSISTENT",
    )

    def __init__(
        self,
        database_path: str | Path,
        stale_after_seconds: int = 300,
    ):
        self.inspector = RecoveryInspector(
            database_path,
            stale_after_seconds=stale_after_seconds,
        )

    def generate(self, now: datetime | None = None) -> dict:
        """
        Generate a structured investigation report.

        Database access is read-only.
        """

        if now is None:
            now = datetime.now(timezone.utc)

        findings = self.inspector.inspect(now=now)

        counts = Counter(
            finding["classification"]
            for finding in findings
        )

        summary = {
            classification: counts.get(classification, 0)
            for classification in self.CLASSIFICATIONS
        }

        requires_attention = [
            finding
            for finding in findings
            if finding["classification"] in {
                "STALE",
                "REVIEW_REQUIRED",
                "INCONSISTENT",
            }
        ]

        return {
            "generated_at": now.astimezone(timezone.utc).isoformat(),
            "total_signals": len(findings),
            "summary": summary,
            "requires_attention": requires_attention,
            "findings": findings,
        }

    @staticmethod
    def to_json(report: dict) -> str:
        """Serialize an investigation report for local review."""

        return json.dumps(
            report,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )