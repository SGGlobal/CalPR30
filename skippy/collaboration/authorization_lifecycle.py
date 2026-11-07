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
databases. Coordinated HALT is a commissioning blocker. This bridge does
not resolve that blocker.
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
    ):
        self.verifier = verifier
        self.policy = policy
        self.protocol = protocol

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
        """

        now = now or datetime.now(timezone.utc)

        if not isinstance(fields, dict):
            return LifecycleResult(False, "Malformed fields", False, None)

        if self.protocol.is_halted:
            return LifecycleResult(
                False,
                "Protocol is halted",
                False,
                None,
            )

        if self.policy.is_halted():
            return LifecycleResult(
                False,
                "Authorization policy is halted",
                False,
                None,
            )

        verification = self.verifier.verify(fields, signature, now=now)
        if not verification.verified:
            return LifecycleResult(
                False,
                verification.reason,
                False,
                self.policy.stored_state(fields.get("authorization_id", "")),
            )

        evidence = self.policy.evaluate_evidence(
            fields.get("authorization_id", ""),
            action=fields.get("action", ""),
            repository=fields.get("repository", ""),
            project_id=fields.get("project_id", ""),
            task_id=fields.get("task_id", ""),
            action_digest=fields.get("action_digest", ""),
            expected_sha=fields.get("expected_sha"),
            environment=fields.get("environment", ""),
            now=now,
        )

        if evidence.state is not AuthorizationState.PENDING:
            return LifecycleResult(
                False,
                f"Policy rejected: {evidence.reason}",
                True,
                evidence.state.value,
            )

        return LifecycleResult(
            True,
            "Synthetic evidence accepted; execution disabled",
            True,
            AuthorizationState.PENDING.value,
        )
