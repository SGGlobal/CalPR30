"""
SKIPPY AI Collaboration Bridge — Offline Authorization Lifecycle.

SYNTHETIC. NON-EXECUTING.

This bridge evaluates verified synthetic identity evidence alongside the
existing authorization policy. A valid signature does not grant real
authorization and does not enable execution.

Consistency limitation:
ChiefIdentityVerifier, AuthorizationPolicy, and CollaborationProtocol use
separate SQLite databases. This bridge does not claim atomicity across
them. If cryptographic verification succeeds and the subsequent policy
evaluation fails, the challenge may already be consumed. That outcome is
fail-closed for execution, but it is not a single atomic transaction.

HALT:
The bridge fails closed if either AuthorizationPolicy or
CollaborationProtocol reports HALT. There is no automatic recovery.
A race remains possible between the two checks because they are separate
databases.

Opt-in coordinated gate:
An optional CoordinatedHaltGate may be supplied. When present, it runs
before identity challenge consumption and policy evaluation. Default
construction leaves the gate unset, so existing behavior is unchanged.
The gate is not system-wide enforcement. Direct component entry points
remain outside it. This is not a production cutover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from skippy.collaboration.authorization import (
    AuthorizationPolicy,
    AuthorizationState,
)
from skippy.collaboration.chief_identity import ChiefIdentityVerifier
from skippy.collaboration.protocol import CollaborationProtocol


@dataclass(frozen=True)
class LifecycleResult:
    accepted: bool
    reason: str
    identity_verified: bool
    policy_state: str | None
    execution_enabled: bool = False


class AuthorizationLifecycle:
    """
    Offline evaluation boundary.

    Does not transition an authorization to a real APPROVED state.
    Does not enable execution.
    """

    def __init__(
        self,
        verifier: ChiefIdentityVerifier,
        policy: AuthorizationPolicy,
        protocol: CollaborationProtocol,
        coordinated_gate=None,
    ):
        self.verifier = verifier
        self.policy = policy
        self.protocol = protocol
        # None preserves existing default behavior.
        self.coordinated_gate = coordinated_gate

    def evaluate(
        self,
        fields: dict | None,
        signature: bytes | None,
        now: datetime | None = None,
    ) -> LifecycleResult:
        """
        Evaluate a signed challenge against the authorization policy.

        Always returns execution_enabled=False.
        Malformed input fails closed without an uncontrolled exception.
        Authorization identifiers are validated before any database query.
        """

        now = now or datetime.now(timezone.utc)

        if not isinstance(fields, dict):
            self._audit(None, "Malformed fields")
            return LifecycleResult(False, "Malformed fields", False, None)

        auth_id = fields.get("authorization_id")
        if not isinstance(auth_id, str) or not auth_id or len(auth_id) > 128:
            self._audit(None, "Malformed authorization identifier")
            return LifecycleResult(
                False, "Malformed authorization identifier", False, None
            )

        # Validate other required fields are present and of acceptable type
        # before any database access. Do not weaken cryptographic checks.
        for key in (
            "action",
            "action_digest",
            "repository",
            "project_id",
            "task_id",
            "environment",
        ):
            value = fields.get(key)
            if not isinstance(value, str) or not value:
                self._audit(auth_id, f"Malformed field: {key}")
                return LifecycleResult(
                    False, f"Malformed field: {key}", False, None
                )

        # Opt-in only. Runs before challenge consumption and policy evaluation.
        # A failed gate is never converted into permission to proceed.
        if self.coordinated_gate is not None:
            gate = self.coordinated_gate.check()
            if not gate.allowed or gate.execution_enabled:
                self._audit(auth_id, f"Coordinated gate rejected: {gate.reason}")
                return LifecycleResult(
                    False,
                    gate.reason,
                    False,
                    None,
                )

        if self.protocol.is_halted:
            self._audit(auth_id, "Protocol is halted")
            return LifecycleResult(
                False,
                "Protocol is halted",
                False,
                None,
            )

        if self.policy.is_halted():
            self._audit(auth_id, "Authorization policy is halted")
            return LifecycleResult(
                False,
                "Authorization policy is halted",
                False,
                None,
            )

        verification = self.verifier.verify(fields, signature, now=now)
        if not verification.verified:
            self._audit(auth_id, f"Cryptographic rejection: {verification.reason}")
            return LifecycleResult(
                False,
                verification.reason,
                False,
                self._safe_stored_state(auth_id),
            )

        evidence = self.policy.evaluate_evidence(
            auth_id,
            action=fields["action"],
            repository=fields["repository"],
            project_id=fields["project_id"],
            task_id=fields["task_id"],
            action_digest=fields["action_digest"],
            expected_sha=fields.get("expected_sha"),
            environment=fields["environment"],
            now=now,
        )

        if evidence.state is not AuthorizationState.PENDING:
            self._audit(auth_id, f"Policy rejected: {evidence.reason}")
            return LifecycleResult(
                False,
                f"Policy rejected: {evidence.reason}",
                True,
                evidence.state.value,
            )

        self._audit(auth_id, "Synthetic evidence accepted; execution disabled")
        return LifecycleResult(
            True,
            "Synthetic evidence accepted; execution disabled",
            True,
            AuthorizationState.PENDING.value,
        )

    def _safe_stored_state(self, authorization_id: str) -> str | None:
        """Query stored state only after identifier validation."""
        try:
            return self.policy.stored_state(authorization_id)
        except Exception:
            return None

    def _audit(self, authorization_id: str | None, reason: str) -> None:
        """Record a non-mutating lifecycle audit event. Never raises."""
        try:
            self.policy.record_lifecycle_audit(authorization_id, reason)
        except Exception:
            pass
