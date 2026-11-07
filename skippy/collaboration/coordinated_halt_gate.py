"""
SKIPPY AI Collaboration Bridge — Coordinated HALT integration safety gate.

OFFLINE. SYNTHETIC. NON-EXECUTING. OPT-IN.

This gate is an isolated integration checkpoint. It is not a production
cutover and does not claim system-wide HALT enforcement.

It performs legacy HALT migration itself, then requires the coordinated
authority to report NOT_HALTED, then re-checks the existing legacy flags.
A caller-supplied migration result is never accepted as authorization.
A failed or incomplete migration is never converted into permission to proceed.

Existing legacy HALT states remain authoritative. A legacy HALT is never
bypassed because the coordinated database reports NOT_HALTED.

execution_enabled remains False. No method grants protected execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skippy.collaboration.authorization import AuthorizationPolicy
from skippy.collaboration.coordinated_halt import CoordinatedHalt, HaltStatus
from skippy.collaboration.legacy_halt_migration import (
    AbsentLegacyPolicy,
    LegacyHaltMigration,
    MigrationError,
    OmittedSourcePolicy,
)
from skippy.collaboration.protocol import CollaborationProtocol


@dataclass(frozen=True)
class GateResult:
    """
    Fail-closed gate outcome.

    execution_enabled is not a constructor argument and cannot be overridden.
    """

    allowed: bool
    reason: str
    coordinated_status: str | None
    migration_completed: bool
    legacy_policy_halted: bool | None
    legacy_protocol_halted: bool | None
    execution_enabled: bool = field(default=False, init=False)


class CoordinatedHaltGate:
    """
    Opt-in safety gate.

    Does not replace component HALT methods. Does not clear legacy flags.
    Does not enable execution. Does not initialize a missing authority.
    """

    execution_enabled = False

    def __init__(
        self,
        coordinated_path,
        policy: AuthorizationPolicy,
        protocol: CollaborationProtocol,
        *,
        absent_legacy_policy: AbsentLegacyPolicy = AbsentLegacyPolicy.ABSENT_LEGACY_IS_UNSAFE,
        omitted_source_policy: OmittedSourcePolicy = OmittedSourcePolicy.OMITTED_SOURCE_IS_UNSAFE,
    ):
        if not isinstance(policy, AuthorizationPolicy):
            raise TypeError("policy must be an AuthorizationPolicy")
        if not isinstance(protocol, CollaborationProtocol):
            raise TypeError("protocol must be a CollaborationProtocol")
        if not isinstance(absent_legacy_policy, AbsentLegacyPolicy):
            raise MigrationError("Invalid absent-legacy policy")
        if not isinstance(omitted_source_policy, OmittedSourcePolicy):
            raise MigrationError("Invalid omitted-source policy")
        self.coordinated_path = coordinated_path
        self.policy = policy
        self.protocol = protocol
        self.absent_legacy_policy = absent_legacy_policy
        self.omitted_source_policy = omitted_source_policy

    def check(self) -> GateResult:
        """
        Validate migration, coordinated NOT_HALTED, and legacy HALT flags.

        Always re-runs migration. Never accepts a caller-supplied result.
        Never raises for expected fail-closed conditions.
        """

        try:
            # Migration is performed here. Its result is not treated as
            # independent authorization; status is re-read below.
            LegacyHaltMigration(
                self.coordinated_path,
                self.policy.database_path,
                self.protocol.database_path,
                absent_legacy_policy=self.absent_legacy_policy,
                omitted_source_policy=self.omitted_source_policy,
            ).migrate()
        except MigrationError as exc:
            return GateResult(
                False,
                f"Migration failed: {exc}",
                None,
                False,
                None,
                None,
            )
        except Exception:
            return GateResult(
                False,
                "Migration failed",
                None,
                False,
                None,
                None,
            )

        try:
            status, _sequence = CoordinatedHalt(self.coordinated_path).read_status()
        except Exception:
            status = HaltStatus.UNKNOWN

        if status is not HaltStatus.NOT_HALTED:
            return GateResult(
                False,
                f"Coordinated HALT state is {status.value}",
                status.value,
                True,
                None,
                None,
            )

        try:
            policy_halted = bool(self.policy.is_halted())
            protocol_halted = bool(self.protocol.is_halted)
        except Exception:
            return GateResult(
                False,
                "Legacy HALT state is unreadable",
                status.value,
                True,
                None,
                None,
            )

        if policy_halted or protocol_halted:
            return GateResult(
                False,
                "Legacy component is halted",
                status.value,
                True,
                policy_halted,
                protocol_halted,
            )

        return GateResult(
            True,
            "Safety conditions satisfied; execution disabled",
            status.value,
            True,
            False,
            False,
        )
