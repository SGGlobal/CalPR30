"""
SKIPPY AI Collaboration Bridge — Authorization Policy Engine.

OFFLINE. NON-EXECUTING.

Chief retains exclusive authority over consequential actions.
AI components may propose, review, or recommend. They must not authorize
or execute protected operations.

A message claiming authorization is not proof of authorization.
An identity string is not authentication.
Execution permission remains disabled in this milestone.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class AuthorizationError(ValueError):
    """Raised when an authorization decision fails closed."""


class AuthorizationState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


class OperationClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    PROTECTED = "PROTECTED"


READ_ONLY_ACTIONS = frozenset({
    "read_repository",
    "read_evidence",
    "local_analysis",
    "local_simulation",
})

PROTECTED_ACTIONS = frozenset({
    "github_write",
    "create_pull_request",
    "modify_branch",
    "merge_pull_request",
    "deploy",
    "railway_config",
    "ifttt_activation",
    "credential_change",
    "permission_change",
    "trading_config",
    "submit_order",
    "financial_transaction",
    "autonomous_execution",
})

KNOWN_PARTICIPANTS = frozenset({"skyppy", "jason", "chief", "bridge"})
AI_PARTICIPANTS = frozenset({"skyppy", "jason", "bridge"})


@dataclass(frozen=True)
class AuthorizationRequest:
    """
    Immutable request for a precisely scoped action.

    chief_identity is a reference, not proof of authentication.
    """

    authorization_id: str
    chief_identity: str
    requesting_participant: str
    action: str
    action_digest: str
    repository: str
    project_id: str
    task_id: str
    expected_sha: str | None
    environment: str
    created_at: datetime
    expires_at: datetime
    state: AuthorizationState = AuthorizationState.PENDING
    consumption_id: str | None = None
    evidence_ref: str | None = None

    def __post_init__(self):
        if not self.authorization_id:
            raise AuthorizationError("authorization_id is required")
        if self.requesting_participant not in KNOWN_PARTICIPANTS:
            raise AuthorizationError("Unknown requesting participant")
        if self.action not in READ_ONLY_ACTIONS | PROTECTED_ACTIONS:
            raise AuthorizationError("Unknown action")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise AuthorizationError("created_at must be timezone-aware")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise AuthorizationError("expires_at must be timezone-aware")


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    state: AuthorizationState
    reason: str
    execution_enabled: bool = False


class AuthorizationPolicy:
    """
    Deny-by-default policy with local audit persistence.

    Execution is always disabled. HALT cannot be overridden.
    """

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        if not self.database_path.parent.is_dir():
            raise AuthorizationError("Database directory does not exist")
        self._init_schema()

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    def _init_schema(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS authorization_requests (
                    authorization_id TEXT PRIMARY KEY,
                    chief_identity TEXT NOT NULL,
                    requesting_participant TEXT NOT NULL,
                    action TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    expected_sha TEXT,
                    environment TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    consumption_id TEXT,
                    evidence_ref TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS authorization_events (
                    event_id TEXT PRIMARY KEY,
                    authorization_id TEXT,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT NOT NULL,
                    reason TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS authorization_control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _record(
        self,
        connection,
        authorization_id: str | None,
        action: str,
        target: str,
        previous: str | None,
        new: str,
        reason: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO authorization_events (
                event_id, authorization_id, occurred_at, action, target,
                previous_state, new_state, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                authorization_id,
                datetime.now(timezone.utc).isoformat(),
                action,
                target,
                previous,
                new,
                reason,
            ),
        )

    def classify(self, action: str) -> OperationClass:
        if action in READ_ONLY_ACTIONS:
            return OperationClass.READ_ONLY
        if action in PROTECTED_ACTIONS:
            return OperationClass.PROTECTED
        raise AuthorizationError("Unknown action")

    def is_halted(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM authorization_control WHERE key = 'halted'"
            ).fetchone()
        return bool(row and row[0] == "1")

    def halt(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO authorization_control (key, value)
                VALUES ('halted', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
                """
            )
            self._record(
                connection, None, "halt", "system", None, "HALTED", "Chief halt"
            )

    def submit(self, request: AuthorizationRequest) -> None:
        if request.requesting_participant in AI_PARTICIPANTS and request.state is AuthorizationState.APPROVED:
            raise AuthorizationError("AI cannot manufacture an approved authorization")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT authorization_id FROM authorization_requests WHERE authorization_id = ?",
                (request.authorization_id,),
            ).fetchone()
            if existing is not None:
                raise AuthorizationError("Authorization identifier already exists")

            connection.execute(
                """
                INSERT INTO authorization_requests (
                    authorization_id, chief_identity, requesting_participant,
                    action, action_digest, repository, project_id, task_id,
                    expected_sha, environment, created_at, expires_at, state,
                    consumption_id, evidence_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.authorization_id,
                    request.chief_identity,
                    request.requesting_participant,
                    request.action,
                    request.action_digest,
                    request.repository,
                    request.project_id,
                    request.task_id,
                    request.expected_sha,
                    request.environment,
                    request.created_at.isoformat(),
                    request.expires_at.isoformat(),
                    request.state.value,
                    request.consumption_id,
                    request.evidence_ref,
                ),
            )
            self._record(
                connection,
                request.authorization_id,
                request.action,
                request.repository,
                None,
                request.state.value,
                "Submitted",
            )

    def decide(
        self,
        authorization_id: str,
        *,
        action: str,
        repository: str,
        project_id: str,
        task_id: str,
        action_digest: str,
        expected_sha: str | None,
        now: datetime | None = None,
    ) -> AuthorizationDecision:
        """
        Validate an authorization request.

        Always returns execution_enabled=False.
        HALT, mismatch, expiration, revocation, and consumption fail closed.
        """

        now = now or datetime.now(timezone.utc)

        if self.is_halted():
            return AuthorizationDecision(
                False, AuthorizationState.DENIED, "System is halted"
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT chief_identity, requesting_participant, action,
                       action_digest, repository, project_id, task_id,
                       expected_sha, expires_at, state, consumption_id
                FROM authorization_requests
                WHERE authorization_id = ?
                """,
                (authorization_id,),
            ).fetchone()

            if row is None:
                self._record(
                    connection, authorization_id, action, repository,
                    None, "DENIED", "Missing authorization",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Missing authorization"
                )

            (
                chief_identity,
                participant,
                stored_action,
                stored_digest,
                stored_repo,
                stored_project,
                stored_task,
                stored_sha,
                expires_at,
                state,
                consumption_id,
            ) = row

            if participant in AI_PARTICIPANTS and chief_identity == participant:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "AI impersonation of Chief",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "AI impersonation of Chief"
                )

            if state == AuthorizationState.REVOKED.value:
                return AuthorizationDecision(
                    False, AuthorizationState.REVOKED, "Authorization revoked"
                )

            if state == AuthorizationState.CONSUMED.value or consumption_id:
                return AuthorizationDecision(
                    False, AuthorizationState.CONSUMED, "Authorization already consumed"
                )

            expires = datetime.fromisoformat(expires_at)
            if now >= expires:
                connection.execute(
                    "UPDATE authorization_requests SET state = ? WHERE authorization_id = ?",
                    (AuthorizationState.EXPIRED.value, authorization_id),
                )
                self._record(
                    connection, authorization_id, action, repository,
                    state, "EXPIRED", "Authorization expired",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.EXPIRED, "Authorization expired"
                )

            if stored_action != action:
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect action"
                )
            if stored_repo != repository:
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect repository"
                )
            if stored_project != project_id or stored_task != task_id:
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect task or project"
                )
            if stored_digest != action_digest:
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Action parameters do not match"
                )
            if stored_sha != expected_sha:
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Commit SHA does not match"
                )

            if state != AuthorizationState.APPROVED.value:
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Authorization is not approved"
                )

            # Even a matching approved request does not enable execution.
            return AuthorizationDecision(
                False,
                AuthorizationState.APPROVED,
                "Validated but execution is disabled",
                execution_enabled=False,
            )

    def revoke(self, authorization_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM authorization_requests WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            if row is None:
                raise AuthorizationError("Missing authorization")
            connection.execute(
                "UPDATE authorization_requests SET state = ? WHERE authorization_id = ?",
                (AuthorizationState.REVOKED.value, authorization_id),
            )
            self._record(
                connection, authorization_id, "revoke", "authorization",
                row[0], "REVOKED", "Revoked",
            )

    def consume(self, authorization_id: str) -> AuthorizationDecision:
        """
        One-time consumption.

        Does not enable execution. Concurrent attempts fail closed because
        the update is conditional on the current state.
        """

        if self.is_halted():
            return AuthorizationDecision(
                False, AuthorizationState.DENIED, "System is halted"
            )

        consumption_id = str(uuid.uuid4())

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE authorization_requests
                SET state = ?, consumption_id = ?
                WHERE authorization_id = ? AND state = ?
                """,
                (
                    AuthorizationState.CONSUMED.value,
                    consumption_id,
                    authorization_id,
                    AuthorizationState.APPROVED.value,
                ),
            )
            if cursor.rowcount != 1:
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    None, "DENIED", "Consumption rejected",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Consumption rejected"
                )

            self._record(
                connection, authorization_id, "consume", "authorization",
                "APPROVED", "CONSUMED", "Consumed; execution still disabled",
            )

        return AuthorizationDecision(
            False,
            AuthorizationState.CONSUMED,
            "Consumed; execution remains disabled",
            execution_enabled=False,
        )
