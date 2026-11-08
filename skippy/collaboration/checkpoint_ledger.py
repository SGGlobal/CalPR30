"""
SKIPPY AI Collaboration Bridge — Offline Checkpoint Handoff Ledger.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Append-only event ledger for checkpoint/state handoff between participants.
Events are evidence and records only. They do not grant permission,
authorize protected actions, or enable execution.

execution_enabled remains False. No automatic HALT clearing.
No cross-database atomicity is claimed. No live adapters.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class LedgerError(ValueError):
    """Raised when a ledger operation fails closed."""


class CheckpointState(str, Enum):
    IMPLEMENTED_REPORTED = "IMPLEMENTED_REPORTED"
    SOURCE_VERIFIED = "SOURCE_VERIFIED"
    CORRECTION_REQUESTED = "CORRECTION_REQUESTED"
    CI_PENDING = "CI_PENDING"
    CI_VERIFIED = "CI_VERIFIED"
    PR_AUTHORIZED = "PR_AUTHORIZED"
    PR_OPENED = "PR_OPENED"
    MERGE_AUTHORIZED = "MERGE_AUTHORIZED"
    MERGED = "MERGED"
    HALTED = "HALTED"
    UNCERTAIN = "UNCERTAIN"


class EventType(str, Enum):
    ASSIGNMENT_CREATED = "ASSIGNMENT_CREATED"
    IMPLEMENTATION_SUBMITTED = "IMPLEMENTATION_SUBMITTED"
    REVIEW_RECORDED = "REVIEW_RECORDED"
    CORRECTION_REQUESTED = "CORRECTION_REQUESTED"
    CORRECTION_SUBMITTED = "CORRECTION_SUBMITTED"
    CI_VERIFICATION_RECORDED = "CI_VERIFICATION_RECORDED"
    PR_AUTHORIZED = "PR_AUTHORIZED"
    PR_OPENED = "PR_OPENED"
    MERGE_AUTHORIZED = "MERGE_AUTHORIZED"
    MERGED = "MERGED"
    HALT = "HALT"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class CheckpointStatus:
    """Read-only projection of current checkpoint state."""

    project_id: str
    task_id: str
    checkpoint_id: str
    state: CheckpointState
    current_sha: str | None
    last_reviewed_sha: str | None
    local_tests_reported: int | None
    ci_verified: bool
    ci_tests: int | None
    outstanding_findings: str | None
    pr_authorized: bool
    merge_authorized: bool
    revision: int
    execution_enabled: bool = field(default=False, init=False)
    rejection_reason: str | None = None


class CheckpointLedger:
    """
    Local-only append-only checkpoint ledger.

    Synthetic. Does not connect to external systems.
    Does not grant permissions. Does not enable execution.
    """

    execution_enabled = False

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        if not self.database_path.parent.is_dir():
            raise LedgerError("Database directory does not exist")
        self._init_schema()

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    def _init_schema(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    message_id TEXT,
                    idempotency_key TEXT,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    expected_revision INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_events_message
                ON events (message_id) WHERE message_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
                ON events (idempotency_key) WHERE idempotency_key IS NOT NULL
                """
            )
            # Initialize revision counter if missing
            connection.execute(
                """
                INSERT OR IGNORE INTO ledger_meta (key, value)
                VALUES ('revision', '0')
                """
            )

    def _current_revision(self, connection) -> int:
        row = connection.execute(
            "SELECT value FROM ledger_meta WHERE key = 'revision'"
        ).fetchone()
        return int(row[0]) if row else 0

    def _next_revision(self, connection) -> int:
        current = self._current_revision(connection)
        new_rev = current + 1
        connection.execute(
            "UPDATE ledger_meta SET value = ? WHERE key = 'revision'",
            (str(new_rev),),
        )
        return new_rev

    def record_event(
        self,
        *,
        event_type: EventType,
        project_id: str,
        task_id: str,
        checkpoint_id: str,
        actor: str,
        payload: dict,
        message_id: str | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """
        Append an event if concurrency and deduplication checks pass.

        Returns the new revision. Raises LedgerError on rejection.
        Does not advance state on failure. Fail-closed.
        """
        now = now or datetime.now(timezone.utc)
        if not isinstance(event_type, EventType):
            raise LedgerError("Invalid event type")
        if not all(isinstance(x, str) and x for x in (project_id, task_id, checkpoint_id, actor)):
            raise LedgerError("Required identity fields missing")

        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            current_rev = self._current_revision(connection)
            if expected_revision is not None and expected_revision != current_rev:
                raise LedgerError("Stale or concurrent revision rejected")

            # Deduplication
            if message_id:
                existing = connection.execute(
                    "SELECT event_id FROM events WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if existing:
                    raise LedgerError("Duplicate message_id")

            if idempotency_key:
                existing = connection.execute(
                    "SELECT event_id FROM events WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    raise LedgerError("Duplicate idempotency_key")

            new_rev = self._next_revision(connection)
            event_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO events (
                    event_id, revision, event_type, project_id, task_id,
                    checkpoint_id, message_id, idempotency_key, actor,
                    payload, occurred_at, expected_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    new_rev,
                    event_type.value,
                    project_id,
                    task_id,
                    checkpoint_id,
                    message_id,
                    idempotency_key,
                    actor,
                    payload_json,
                    now.isoformat(),
                    expected_revision,
                ),
            )
            return new_rev

    def status(self, project_id: str, task_id: str) -> CheckpointStatus | None:
        """
        Deterministic read-only projection of current state from durable events.
        Returns None if no events exist for the checkpoint.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_type, checkpoint_id, payload, revision
                FROM events
                WHERE project_id = ? AND task_id = ?
                ORDER BY revision ASC
                """,
                (project_id, task_id),
            ).fetchall()

        if not rows:
            return None

        state = CheckpointState.UNCERTAIN
        current_sha = None
        last_reviewed_sha = None
        local_tests = None
        ci_verified = False
        ci_tests = None
        findings = None
        pr_auth = False
        merge_auth = False
        checkpoint_id = rows[-1][1]
        revision = rows[-1][3]

        for event_type, cp_id, payload_json, rev in rows:
            payload = json.loads(payload_json)
            et = EventType(event_type)

            if et == EventType.IMPLEMENTATION_SUBMITTED:
                state = CheckpointState.IMPLEMENTED_REPORTED
                current_sha = payload.get("sha")
                local_tests = payload.get("local_tests")
            elif et == EventType.REVIEW_RECORDED:
                last_reviewed_sha = payload.get("sha") or current_sha
            elif et == EventType.CORRECTION_REQUESTED:
                state = CheckpointState.CORRECTION_REQUESTED
                findings = payload.get("findings")
            elif et == EventType.CORRECTION_SUBMITTED:
                state = CheckpointState.IMPLEMENTED_REPORTED
                current_sha = payload.get("sha")
            elif et == EventType.CI_VERIFICATION_RECORDED:
                state = CheckpointState.CI_VERIFIED
                ci_verified = True
                ci_tests = payload.get("ci_tests")
            elif et == EventType.PR_AUTHORIZED:
                pr_auth = True
                if state not in (CheckpointState.HALTED, CheckpointState.UNCERTAIN):
                    state = CheckpointState.PR_AUTHORIZED
            elif et == EventType.MERGE_AUTHORIZED:
                merge_auth = True
                if state not in (CheckpointState.HALTED, CheckpointState.UNCERTAIN):
                    state = CheckpointState.MERGE_AUTHORIZED
            elif et == EventType.MERGED:
                state = CheckpointState.MERGED
            elif et == EventType.HALT:
                state = CheckpointState.HALTED
            elif et == EventType.UNCERTAIN:
                state = CheckpointState.UNCERTAIN

        return CheckpointStatus(
            project_id=project_id,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            state=state,
            current_sha=current_sha,
            last_reviewed_sha=last_reviewed_sha,
            local_tests_reported=local_tests,
            ci_verified=ci_verified,
            ci_tests=ci_tests,
            outstanding_findings=findings,
            pr_authorized=pr_auth,
            merge_authorized=merge_auth,
            revision=revision,
        )
