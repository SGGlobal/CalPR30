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
databases. Coordinated HALT is a commissioning blocker.
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
        fields: dict,
        signature: bytes | None,
        now: datetime | None = None,
    ) -> LifecycleResult:
        """
        Evaluate a signed challenge against the authorization policy.

        Always returns execution_enabled=False.
        """

        now = now or datetime.now(timezone.utc)

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

        decision = self.policy.decide(
            fields["authorization_id"],
            action=fields["action"],
            repository=fields["repository"],
            project_id=fields["project_id"],
            task_id=fields["task_id"],
            action_digest=fields["action_digest"],
            expected_sha=fields["expected_sha"],
            environment=fields["environment"],
            now=now,
        )

        if decision.state is not AuthorizationState.PENDING:
            return LifecycleResult(
                False,
                f"Policy rejected: {decision.reason}",
                True,
                decision.state.value,
            )

        # A matching pending request with a valid synthetic signature is
        # evidence only. It does not transition the authorization and does
        # not enable execution.
        return LifecycleResult(
            True,
            "Synthetic evidence accepted; execution disabled",
            True,
            AuthorizationState.PENDING.value,
        )
