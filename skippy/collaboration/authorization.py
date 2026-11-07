"""
SKIPPY AI Collaboration Bridge — Authorization Policy Engine.

OFFLINE. NON-EXECUTING.

Chief retains exclusive authority over consequential actions.
AI components may propose, review, or recommend. They must not authorize
or execute protected operations.

A message claiming authorization is not proof of authorization.
An identity string is not authentication.
Execution permission remains disabled in this milestone.

HALT limitation (commissioning blocker):
AuthorizationPolicy and CollaborationProtocol each maintain an independent
persistent HALT flag in separate databases. This policy does NOT
automatically recognize a HALT issued through CollaborationProtocol.halt().
Coordinated HALT enforcement across both components is a commissioning
blocker and is reserved for a separately reviewed integration milestone.
Until then, this policy fails closed only on its own halt() flag.
There is no automatic resume and no recovery procedure in this step.
External execution remains disabled while this limitation is unresolved.

Terminal states:
CONSUMED, REVOKED, EXPIRED, and DENIED are terminal. They are not
overwritten by later transitions. Audit events record rejected attempts
without changing the stored state.
"""

from __future__ import annotations

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

TERMINAL_STATES = frozenset({
    AuthorizationState.CONSUMED.value,
    AuthorizationState.REVOKED.value,
    AuthorizationState.EXPIRED.value,
    AuthorizationState.DENIED.value,
})


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
        if not isinstance(self.authorization_id, str) or not self.authorization_id:
            raise AuthorizationError("authorization_id is required")
        if not isinstance(self.chief_identity, str) or not self.chief_identity:
            raise AuthorizationError("chief_identity is required")
        if self.requesting_participant not in KNOWN_PARTICIPANTS:
            raise AuthorizationError("Unknown requesting participant")
        if self.action not in READ_ONLY_ACTIONS | PROTECTED_ACTIONS:
            raise AuthorizationError("Unknown action")
        if not isinstance(self.action_digest, str) or not self.action_digest:
            raise AuthorizationError("action_digest is required")
        if not isinstance(self.repository, str) or not self.repository:
            raise AuthorizationError("repository is required")
        if not isinstance(self.project_id, str) or not self.project_id:
            raise AuthorizationError("project_id is required")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise AuthorizationError("task_id is required")
        if not isinstance(self.environment, str) or not self.environment:
            raise AuthorizationError("environment is required")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise AuthorizationError("created_at must be timezone-aware")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise AuthorizationError("expires_at must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise AuthorizationError("expires_at must be after created_at")


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    state: AuthorizationState
    reason: str
    execution_enabled: bool = False


class AuthorizationPolicy:
    """
    Deny-by-default policy with local audit persistence.

    Execution is always disabled. This policy's HALT cannot be overridden.
    It does not automatically observe CollaborationProtocol HALT.

    Opt-in coordinated guard:
    require_coordinated_guard() may bind a CoordinatedHaltGate that
    references this same policy instance. Once established, the guard
    cannot be replaced through supported methods. Default construction
    remains ungated. This is not system-wide enforcement and not a
    production cutover. Administrative halt() and revoke() remain
    available as protective operations.
    """

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        if not self.database_path.parent.is_dir():
            raise AuthorizationError("Database directory does not exist")
        self._init_schema()
        self._coordinated_guard = None

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

    def events_for(self, authorization_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT reason FROM authorization_events
                WHERE authorization_id = ?
                ORDER BY occurred_at
                """,
                (authorization_id,),
            ).fetchall()
        return [row[0] for row in rows]

    def record_lifecycle_audit(
        self,
        authorization_id: str | None,
        reason: str,
        *,
        action: str = "lifecycle_evaluation",
        target: str = "authorization",
    ) -> None:
        """
        Narrowly scoped, non-mutating audit for lifecycle evaluation outcomes.

        Does not change authorization state.
        Does not store signatures, private keys, or sensitive payloads.
        Uses parameterized queries. Persists across restarts.
        Not claimed to be tamper-proof.
        """
        if authorization_id is not None and not isinstance(authorization_id, str):
            authorization_id = None
        if not isinstance(reason, str) or not reason:
            reason = "Unspecified lifecycle outcome"

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record(
                connection,
                authorization_id,
                action,
                target,
                None,
                "AUDITED",
                reason,
            )

    def stored_state(self, authorization_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM authorization_requests WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
        return row[0] if row else None

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

    def require_coordinated_guard(self, gate) -> None:
        """
        Bind an opt-in CoordinatedHaltGate to this policy instance.

        The guard must reference this same policy. It cannot be removed
        or replaced through this supported method. Default construction
        remains ungated.
        """

        from skippy.collaboration.coordinated_halt_gate import CoordinatedHaltGate

        if self._coordinated_guard is not None:
            raise AuthorizationError("Coordinated guard cannot be replaced")
        if not isinstance(gate, CoordinatedHaltGate):
            raise AuthorizationError("Invalid coordinated guard")
        if gate.policy is not self:
            raise AuthorizationError("Coordinated guard policy mismatch")
        self._coordinated_guard = gate

    @property
    def coordinated_guard(self):
        """Read-only view of the established guard, if any."""
        return self._coordinated_guard

    def _guard_rejection(self) -> str | None:
        """
        Run the opt-in guard before any local write lock.

        Returns a controlled rejection reason, or None when clear.
        Never exposes exception details. Never treats failure as permission.
        """

        guard = self._coordinated_guard
        if guard is None:
            return None
        try:
            from skippy.collaboration.coordinated_halt_gate import (
                CoordinatedHaltGate,
                GateResult,
            )

            if not isinstance(guard, CoordinatedHaltGate) or guard.policy is not self:
                return "Coordinated guard rejected"
            result = guard.check()
            if not isinstance(result, GateResult):
                return "Coordinated guard rejected"
            if (
                result.allowed is not True
                or result.execution_enabled is not False
                or result.coordinated_status != "NOT_HALTED"
                or result.migration_completed is not True
                or result.legacy_policy_halted is not False
                or result.legacy_protocol_halted is not False
            ):
                return "Coordinated guard rejected"
            return None
        except Exception:
            return "Coordinated guard rejected"

    def _audit_guard_rejection(
        self,
        authorization_id: str | None,
        action: str,
        target: str,
        reason: str,
    ) -> None:
        """Best-effort audit. Does not change authorization state."""
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._record(
                    connection,
                    authorization_id,
                    action,
                    target,
                    None,
                    "DENIED",
                    reason,
                )
        except Exception:
            return

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
        if (
            request.requesting_participant in AI_PARTICIPANTS
            and request.state is AuthorizationState.APPROVED
        ):
            raise AuthorizationError(
                "AI cannot manufacture an approved authorization"
            )

        rejection = self._guard_rejection()
        if rejection:
            self._audit_guard_rejection(
                request.authorization_id, request.action, request.repository, rejection
            )
            raise AuthorizationError(rejection)

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
        environment: str,
        now: datetime | None = None,
    ) -> AuthorizationDecision:
        """
        Validate an authorization request.

        Always returns execution_enabled=False.
        HALT, mismatch, expiration, revocation, and consumption fail closed.
        """

        now = now or datetime.now(timezone.utc)

        rejection = self._guard_rejection()
        if rejection:
            self._audit_guard_rejection(authorization_id, action, repository, rejection)
            return AuthorizationDecision(
                False, AuthorizationState.DENIED, rejection
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            halt_row = connection.execute(
                "SELECT value FROM authorization_control WHERE key = 'halted'"
            ).fetchone()
            if halt_row and halt_row[0] == "1":
                self._record(
                    connection, authorization_id, action, repository,
                    None, "DENIED", "System is halted",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "System is halted"
                )

            row = connection.execute(
                """
                SELECT chief_identity, requesting_participant, action,
                       action_digest, repository, project_id, task_id,
                       expected_sha, environment, expires_at, state,
                       consumption_id
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
                stored_environment,
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
                self._record(
                    connection, authorization_id, action, repository,
                    state, "REVOKED", "Authorization revoked",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.REVOKED, "Authorization revoked"
                )

            if state == AuthorizationState.CONSUMED.value or consumption_id:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "CONSUMED", "Authorization already consumed",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.CONSUMED, "Authorization already consumed"
                )

            if state == AuthorizationState.DENIED.value:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Authorization denied",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Authorization denied"
                )

            expires = datetime.fromisoformat(expires_at)
            if now >= expires:
                if state not in TERMINAL_STATES:
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
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect action",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect action"
                )
            if stored_repo != repository:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect repository",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect repository"
                )
            if stored_project != project_id or stored_task != task_id:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect task or project",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect task or project"
                )
            if stored_digest != action_digest:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Action parameters do not match",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Action parameters do not match"
                )
            if stored_sha != expected_sha:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Commit SHA does not match",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Commit SHA does not match"
                )
            if stored_environment != environment:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect environment",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect environment"
                )

            if state != AuthorizationState.APPROVED.value:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Authorization is not approved",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Authorization is not approved"
                )

            self._record(
                connection, authorization_id, action, repository,
                state, "APPROVED", "Validated; execution disabled",
            )
            return AuthorizationDecision(
                False,
                AuthorizationState.APPROVED,
                "Validated but execution is disabled",
                execution_enabled=False,
            )

    def evaluate_evidence(
        self,
        authorization_id: str,
        *,
        action: str,
        repository: str,
        project_id: str,
        task_id: str,
        action_digest: str,
        expected_sha: str | None,
        environment: str,
        now: datetime | None = None,
    ) -> AuthorizationDecision:
        """
        Non-executing evaluation of PENDING authorization evidence.

        Reads the trusted stored record inside a transaction.
        Requires state == PENDING.
        Validates exact scope match.
        Does not transition the state.
        Always returns execution_enabled=False.
        Honors HALT. Rejects terminal states, unknowns, and impersonation.
        """

        now = now or datetime.now(timezone.utc)

        rejection = self._guard_rejection()
        if rejection:
            self._audit_guard_rejection(authorization_id, action, repository, rejection)
            return AuthorizationDecision(
                False, AuthorizationState.DENIED, rejection
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            halt_row = connection.execute(
                "SELECT value FROM authorization_control WHERE key = 'halted'"
            ).fetchone()
            if halt_row and halt_row[0] == "1":
                self._record(
                    connection, authorization_id, action, repository,
                    None, "DENIED", "System is halted",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "System is halted"
                )

            row = connection.execute(
                """
                SELECT chief_identity, requesting_participant, action,
                       action_digest, repository, project_id, task_id,
                       expected_sha, environment, expires_at, state,
                       consumption_id
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
                stored_environment,
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

            if state != AuthorizationState.PENDING.value:
                reason = f"Authorization is not PENDING (state={state})"
                self._record(
                    connection, authorization_id, action, repository,
                    state, state, reason,
                )
                return AuthorizationDecision(
                    False, AuthorizationState(state), reason
                )

            if consumption_id:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "CONSUMED", "Authorization already consumed",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.CONSUMED, "Authorization already consumed"
                )

            expires = datetime.fromisoformat(expires_at)
            if now >= expires:
                if state not in TERMINAL_STATES:
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
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect action",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect action"
                )
            if stored_repo != repository:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect repository",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect repository"
                )
            if stored_project != project_id or stored_task != task_id:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect task or project",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect task or project"
                )
            if stored_digest != action_digest:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Action parameters do not match",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Action parameters do not match"
                )
            if stored_sha != expected_sha:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Commit SHA does not match",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Commit SHA does not match"
                )
            if stored_environment != environment:
                self._record(
                    connection, authorization_id, action, repository,
                    state, "DENIED", "Incorrect environment",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Incorrect environment"
                )

            # Matching PENDING evidence. Do not transition state.
            self._record(
                connection, authorization_id, action, repository,
                state, state, "Synthetic evidence accepted",
            )
            return AuthorizationDecision(
                False,
                AuthorizationState.PENDING,
                "Synthetic evidence accepted",
                execution_enabled=False,
            )

    def revoke(self, authorization_id: str) -> AuthorizationDecision:
        """
        Revoke a non-terminal authorization.

        Terminal states are not overwritten. The rejection is audited.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM authorization_requests WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            if row is None:
                raise AuthorizationError("Missing authorization")

            state = row[0]
            if state in TERMINAL_STATES:
                self._record(
                    connection, authorization_id, "revoke", "authorization",
                    state, state, "Terminal state cannot be overwritten",
                )
                return AuthorizationDecision(
                    False,
                    AuthorizationState(state),
                    "Terminal state cannot be overwritten",
                )

            connection.execute(
                "UPDATE authorization_requests SET state = ? WHERE authorization_id = ?",
                (AuthorizationState.REVOKED.value, authorization_id),
            )
            self._record(
                connection, authorization_id, "revoke", "authorization",
                state, "REVOKED", "Revoked",
            )

        return AuthorizationDecision(
            False, AuthorizationState.REVOKED, "Revoked"
        )

    def consume(
        self,
        authorization_id: str,
        now: datetime | None = None,
    ) -> AuthorizationDecision:
        """
        One-time consumption.

        HALT, expiration, revocation, denial, and prior consumption are
        checked inside the same transaction. Every rejection is audited.
        Does not enable execution. Concurrent attempts fail closed.
        Terminal states are not overwritten merely to record a rejection.
        """

        now = now or datetime.now(timezone.utc)
        consumption_id = str(uuid.uuid4())

        rejection = self._guard_rejection()
        if rejection:
            self._audit_guard_rejection(
                authorization_id, "consume", "authorization", rejection
            )
            return AuthorizationDecision(
                False, AuthorizationState.DENIED, rejection
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            halt_row = connection.execute(
                "SELECT value FROM authorization_control WHERE key = 'halted'"
            ).fetchone()
            if halt_row and halt_row[0] == "1":
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    None, "DENIED", "System is halted",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "System is halted"
                )

            row = connection.execute(
                """
                SELECT state, expires_at, consumption_id
                FROM authorization_requests
                WHERE authorization_id = ?
                """,
                (authorization_id,),
            ).fetchone()

            if row is None:
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    None, "DENIED", "Missing authorization",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Missing authorization"
                )

            state, expires_at, existing_consumption = row

            if state == AuthorizationState.REVOKED.value:
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    state, "REVOKED", "Authorization revoked",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.REVOKED, "Authorization revoked"
                )

            if state == AuthorizationState.CONSUMED.value or existing_consumption:
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    state, "CONSUMED", "Authorization already consumed",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.CONSUMED, "Authorization already consumed"
                )

            if state == AuthorizationState.DENIED.value:
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    state, "DENIED", "Authorization denied",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Authorization denied"
                )

            expires = datetime.fromisoformat(expires_at)
            if now >= expires:
                connection.execute(
                    "UPDATE authorization_requests SET state = ? WHERE authorization_id = ?",
                    (AuthorizationState.EXPIRED.value, authorization_id),
                )
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    state, "EXPIRED", "Expired during consumption",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.EXPIRED, "Authorization expired"
                )

            if state != AuthorizationState.APPROVED.value:
                self._record(
                    connection, authorization_id, "consume", "authorization",
                    state, "DENIED", "Invalid state for consumption",
                )
                return AuthorizationDecision(
                    False, AuthorizationState.DENIED, "Invalid state for consumption"
                )

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
                    state, "DENIED", "Consumption rejected",
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
