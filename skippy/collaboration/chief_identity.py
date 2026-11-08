"""
SKIPPY AI Collaboration Bridge — Offline Chief Identity Verifier.

SYNTHETIC. NON-EXECUTING.

A valid signature establishes possession of a signing key. It does not
establish informed human consent, and it does not enable execution.

Challenges originate from this verifier. A signature is accepted only when
it matches the cryptographic payload and every trusted challenge field,
including timestamps and the intended key identifier.

register_public_key() is a synthetic, test-facing configuration method.
It does not establish authenticated Chief enrollment. It must not be
exposed through a production collaboration interface without a separately
reviewed trusted enrollment mechanism.

The optional `now` argument exists for deterministic tests. Any future
production verification entrypoint must obtain time from a trusted local
clock. An untrusted caller must not be able to override that clock to
revive an expired authorization.

The verifier does not provide a general-purpose signing capability.
Private keys are never persisted by this module.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


PROTOCOL_VERSION = "chief-auth-1"
DOMAIN = "skippy.chief.authorization"
APPLICATION = "skippy-ai-collaboration-bridge"
MAX_CHALLENGE_LIFETIME = timedelta(minutes=15)
SHA_LENGTH = 40


class IdentityError(ValueError):
    """Raised when verification fails closed."""


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    reason: str
    execution_enabled: bool = False


REQUIRED_FIELDS = (
    "authorization_id",
    "application",
    "protocol_version",
    "action",
    "action_digest",
    "repository",
    "environment",
    "project_id",
    "task_id",
    "expected_sha",
    "key_id",
    "challenge_id",
    "nonce",
    "issued_at",
    "expires_at",
)

SCOPE_FIELDS = (
    "authorization_id",
    "action",
    "action_digest",
    "repository",
    "environment",
    "project_id",
    "task_id",
    "expected_sha",
    "key_id",
    "nonce",
    "issued_at",
    "expires_at",
)


def _parse_timestamp(value: str, name: str) -> datetime:
    if not isinstance(value, str):
        raise IdentityError(f"{name} must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IdentityError(f"Malformed {name}") from exc
    if parsed.tzinfo is None:
        raise IdentityError(f"{name} must be timezone-aware")
    return parsed


def _valid_sha(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def canonical_payload(fields: dict) -> bytes:
    """
    Deterministic UTF-8 serialization with domain separation.

    Unknown fields fail closed. Field order is fixed.
    """

    if not isinstance(fields, dict):
        raise IdentityError("Payload must be a dictionary")

    unknown = set(fields) - set(REQUIRED_FIELDS)
    if unknown:
        raise IdentityError("Unexpected payload field")

    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    if missing:
        raise IdentityError("Missing payload field")

    for name in REQUIRED_FIELDS:
        if not isinstance(fields[name], str) or not fields[name]:
            raise IdentityError(f"{name} must be a nonempty string")

    if fields["application"] != APPLICATION:
        raise IdentityError("Unexpected application")
    if fields["protocol_version"] != PROTOCOL_VERSION:
        raise IdentityError("Unsupported protocol version")

    issued = _parse_timestamp(fields["issued_at"], "issued_at")
    expires = _parse_timestamp(fields["expires_at"], "expires_at")
    if expires <= issued:
        raise IdentityError("expires_at must be after issued_at")
    if expires - issued > MAX_CHALLENGE_LIFETIME:
        raise IdentityError("Challenge lifetime exceeds maximum")

    if not _valid_sha(fields["expected_sha"]):
        raise IdentityError("expected_sha is invalid")
    if len(fields["nonce"]) < 16:
        raise IdentityError("nonce is invalid")

    ordered = {name: fields[name] for name in REQUIRED_FIELDS}
    body = json.dumps(
        ordered,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"{DOMAIN}\n{PROTOCOL_VERSION}\n{body}".encode("utf-8")


class ChiefIdentityVerifier:
    """
    Offline signature verifier with a trusted challenge registry.

    Challenges originate here. Does not sign. Does not enable execution.
    """

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        if not self.database_path.parent.is_dir():
            raise IdentityError("Database directory does not exist")
        self._init_schema()
        # Explicit opt-in coordinated guard. Default remains ungated.
        # Ungated identity operations that produce work-related evidence
        # fail closed. This binding does not create a separate authority.
        self._coordinated_guard = None
        self._coordinated_enforcement_required = False
        self._coordinated_guard_snapshot = None

    def _connect(self):
        return sqlite3.connect(self.database_path, timeout=5.0)

    def _init_schema(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_keys (
                    key_id TEXT PRIMARY KEY,
                    public_key BLOB NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS challenges (
                    challenge_id TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL UNIQUE,
                    authorization_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    expected_sha TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

    def register_public_key(self, key_id: str, public_key: bytes) -> None:
        """
        Register a synthetic trusted public key.

        Test-facing and isolated. Does not accept a private key and does
        not enroll an AI identity as Chief. Duplicate key identifiers are
        rejected. This is not authenticated Chief enrollment.
        """

        if not isinstance(key_id, str) or not key_id:
            raise IdentityError("key_id is required")
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise IdentityError("public key must be 32 bytes")

        Ed25519PublicKey.from_public_bytes(public_key)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT key_id FROM trusted_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            if existing is not None:
                raise IdentityError("Duplicate key identifier")
            connection.execute(
                """
                INSERT INTO trusted_keys (key_id, public_key, status)
                VALUES (?, ?, 'active')
                """,
                (key_id, public_key),
            )

    def revoke(self, key_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE trusted_keys SET status = 'revoked' WHERE key_id = ?",
                (key_id,),
            )
            if cursor.rowcount != 1:
                raise IdentityError("Unknown key")

    def require_coordinated_guard(self, gate) -> None:
        """
        Bind an explicit CoordinatedHaltGate to this identity verifier.

        Requires a genuine CoordinatedHaltGate. Rejects subclasses,
        replaced check methods, and mismatched policy/protocol identities.
        Requires the policy and protocol to be already bound to the same
        gate. Snapshots configuration. Cannot be replaced. Default
        construction remains ungated; ungated work-related operations
        fail closed. Does not create an independent security authority.
        """

        from skippy.collaboration.coordinated_halt_gate import CoordinatedHaltGate

        if self._coordinated_guard is not None or self._coordinated_enforcement_required:
            raise IdentityError("Coordinated guard cannot be replaced")
        if type(gate) is not CoordinatedHaltGate:
            raise IdentityError("Invalid coordinated guard type")
        if getattr(gate.check, "__func__", None) is not CoordinatedHaltGate.check:
            raise IdentityError("Coordinated gate method was replaced")
        # Verify policy and protocol are the genuine ones bound to this gate
        if gate.policy is None or gate.protocol is None:
            raise IdentityError("Coordinated guard missing policy or protocol")
        if not getattr(gate.policy, "coordinated_enforcement_required", False):
            raise IdentityError("Policy not bound to coordinated guard")
        if gate.policy.coordinated_guard is not gate:
            raise IdentityError("Policy guard identity mismatch")
        if not getattr(gate.protocol, "coordinated_enforcement_required", False):
            raise IdentityError("Protocol not bound to coordinated guard")
        if gate.protocol.coordinated_guard is not gate:
            raise IdentityError("Protocol guard identity mismatch")
        # Snapshot security-relevant configuration
        from pathlib import Path

        self._coordinated_guard = gate
        self._coordinated_enforcement_required = True
        self._coordinated_guard_snapshot = {
            "guard": gate,
            "policy": gate.policy,
            "protocol": gate.protocol,
            "policy_path": Path(gate.policy.database_path),
            "protocol_path": Path(gate.protocol.database_path),
            "coordinated_path": Path(gate.coordinated_path),
            "absent_legacy_policy": gate.absent_legacy_policy,
            "omitted_source_policy": gate.omitted_source_policy,
        }

    @property
    def coordinated_guard(self):
        """Read-only view of the established guard, if any."""
        return self._coordinated_guard

    @property
    def coordinated_enforcement_required(self) -> bool:
        """True after explicit binding. Does not itself prove validity."""
        return self._coordinated_enforcement_required

    def _identity_guard_rejection(self) -> str | None:
        """
        Enforce coordinated HALT protection for identity work operations.

        Returns a controlled rejection reason, or None when protection
        is valid and current evaluation succeeds. Never exposes internal
        exception details. Never treats failure as permission.
        Ungated (never bound) rejects. Rejects drift, replacement,
        incomplete migration, HALTED, UNKNOWN, etc.
        """

        if not self._coordinated_enforcement_required:
            return "Missing coordinated guard"
        guard = self._coordinated_guard
        snapshot = self._coordinated_guard_snapshot
        if guard is None or snapshot is None or guard is not snapshot["guard"]:
            return "Removed or replaced coordinated binding"
        try:
            from skippy.collaboration.coordinated_halt_gate import (
                CoordinatedHaltGate,
                GateResult,
            )
            from pathlib import Path

            if type(guard) is not CoordinatedHaltGate:
                return "Invalid guard type"
            if getattr(guard.check, "__func__", None) is not CoordinatedHaltGate.check:
                return "Replaced check method"
            if guard.policy is not snapshot["policy"] or guard.policy is None:
                return "Policy identity mismatch"
            if guard.protocol is not snapshot["protocol"] or guard.protocol is None:
                return "Protocol identity mismatch"
            if not guard.policy.coordinated_enforcement_required:
                return "Policy enforcement not required"
            if guard.policy.coordinated_guard is not guard:
                return "Policy guard identity mismatch"
            if not guard.protocol.coordinated_enforcement_required:
                return "Protocol enforcement not required"
            if guard.protocol.coordinated_guard is not guard:
                return "Protocol guard identity mismatch"
            if Path(guard.policy.database_path) != snapshot["policy_path"]:
                return "Configuration drift: policy path"
            if Path(guard.protocol.database_path) != snapshot["protocol_path"]:
                return "Configuration drift: protocol path"
            if Path(guard.coordinated_path) != snapshot["coordinated_path"]:
                return "Configuration drift: coordinated path"
            if guard.absent_legacy_policy != snapshot["absent_legacy_policy"]:
                return "Configuration drift: absent legacy policy"
            if guard.omitted_source_policy != snapshot["omitted_source_policy"]:
                return "Configuration drift: omitted source policy"
            # Current safety evaluation
            result = guard.check()
            if not isinstance(result, GateResult):
                return "Invalid guard result"
            if result.execution_enabled is not False:
                return "execution_enabled is not False"
            if not result.allowed:
                # Map specific conditions without leaking internals
                reason = result.reason or "Coordinated protection rejected"
                status = result.coordinated_status
                if status == "HALTED":
                    return "Coordinated HALTED"
                if status == "UNKNOWN" or (status is None and "UNKNOWN" in reason.upper()):
                    return "Coordinated UNKNOWN"
                if "Migration" in reason or result.migration_completed is not True:
                    return "Failed migration"
                if result.legacy_policy_halted is True:
                    return "Legacy policy HALT"
                if result.legacy_protocol_halted is True:
                    return "Legacy protocol HALT"
                if "authority" in reason.lower() or status is None:
                    return "Missing or corrupted coordinated authority"
                return "Coordinated protection rejected"
            # Allowed must be consistent
            if (
                result.coordinated_status != "NOT_HALTED"
                or result.migration_completed is not True
                or result.legacy_policy_halted is not False
                or result.legacy_protocol_halted is not False
            ):
                return "Invalid or inconsistent gate results"
            return None
        except Exception:
            return "Coordinated protection evaluation failed"

    def issue_challenge(
        self,
        *,
        authorization_id: str,
        action: str,
        action_digest: str,
        repository: str,
        environment: str,
        project_id: str,
        task_id: str,
        expected_sha: str,
        key_id: str,
        now: datetime | None = None,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> dict:
        """
        Issue a trusted challenge bound to an exact authorization scope
        and an active signing key.

        The returned challenge already contains the trusted key identifier.
        Callers cannot modify the stored challenge after issuance.
        """

        now = now or datetime.now(timezone.utc)
        if lifetime <= timedelta(0) or lifetime > MAX_CHALLENGE_LIFETIME:
            raise IdentityError("Invalid challenge lifetime")

        # Coordinated HALT protection must be established and current
        # before any challenge record is created.
        rejection = self._identity_guard_rejection()
        if rejection is not None:
            raise IdentityError(rejection)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            key = connection.execute(
                "SELECT status FROM trusted_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            if key is None:
                raise IdentityError("Unknown signing key")
            if key[0] != "active":
                raise IdentityError("Signing key is not active")

            challenge = {
                "authorization_id": authorization_id,
                "application": APPLICATION,
                "protocol_version": PROTOCOL_VERSION,
                "action": action,
                "action_digest": action_digest,
                "repository": repository,
                "environment": environment,
                "project_id": project_id,
                "task_id": task_id,
                "expected_sha": expected_sha,
                "key_id": key_id,
                "challenge_id": uuid.uuid4().hex,
                "nonce": uuid.uuid4().hex,
                "issued_at": now.isoformat(),
                "expires_at": (now + lifetime).isoformat(),
            }
            canonical_payload(challenge)

            connection.execute(
                """
                INSERT INTO challenges (
                    challenge_id, nonce, authorization_id, action,
                    action_digest, repository, environment, project_id,
                    task_id, expected_sha, key_id, issued_at, expires_at,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    challenge["challenge_id"],
                    challenge["nonce"],
                    authorization_id,
                    action,
                    action_digest,
                    repository,
                    environment,
                    project_id,
                    task_id,
                    expected_sha,
                    key_id,
                    challenge["issued_at"],
                    challenge["expires_at"],
                ),
            )
        return challenge

    def verify(
        self,
        fields: dict,
        signature: bytes | None,
        now: datetime | None = None,
    ) -> VerificationResult:
        """
        Verify a signature against the trusted challenge scope.

        Timestamps and key identifier are compared against the registry.
        Expiration is evaluated from the trusted registry timestamp.
        Always returns execution_enabled=False.

        The `now` argument is for tests. Production verification must use
        a trusted local clock.
        """

        now = now or datetime.now(timezone.utc)

        # Require valid coordinated HALT protection before consuming
        # a challenge or returning successful verification.
        # An unbound verifier cannot return successful work-related
        # verification. Rejection does not consume the challenge.
        rejection = self._identity_guard_rejection()
        if rejection is not None:
            return VerificationResult(False, rejection)

        try:
            payload = canonical_payload(fields)
        except IdentityError as exc:
            return VerificationResult(False, str(exc))

        if not isinstance(signature, bytes) or len(signature) != 64:
            return VerificationResult(False, "Malformed signature")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            challenge = connection.execute(
                """
                SELECT authorization_id, action, action_digest, repository,
                       environment, project_id, task_id, expected_sha,
                       key_id, nonce, issued_at, expires_at, status
                FROM challenges
                WHERE challenge_id = ?
                """,
                (fields["challenge_id"],),
            ).fetchone()

            if challenge is None:
                return VerificationResult(False, "Unknown challenge")

            (
                authorization_id,
                action,
                action_digest,
                repository,
                environment,
                project_id,
                task_id,
                expected_sha,
                key_id,
                nonce,
                issued_at,
                expires_at,
                status,
            ) = challenge

            if status != "open":
                return VerificationResult(False, "Challenge already consumed")

            trusted_expires = datetime.fromisoformat(expires_at)
            if now >= trusted_expires:
                return VerificationResult(False, "Challenge expired")

            trusted_issued = datetime.fromisoformat(issued_at)
            if trusted_issued > now:
                return VerificationResult(False, "Challenge issued in the future")

            scope = {
                "authorization_id": authorization_id,
                "action": action,
                "action_digest": action_digest,
                "repository": repository,
                "environment": environment,
                "project_id": project_id,
                "task_id": task_id,
                "expected_sha": expected_sha,
                "key_id": key_id,
                "nonce": nonce,
                "issued_at": issued_at,
                "expires_at": expires_at,
            }
            for name in SCOPE_FIELDS:
                if fields[name] != scope[name]:
                    return VerificationResult(
                        False, f"Scope mismatch: {name}"
                    )

            key = connection.execute(
                "SELECT public_key, status FROM trusted_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()

            if key is None:
                return VerificationResult(False, "Unknown public key")

            public_bytes, key_status = key
            if key_status != "active":
                return VerificationResult(False, "Key revoked")

            public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
            try:
                public_key.verify(signature, payload)
            except InvalidSignature:
                return VerificationResult(False, "Invalid signature")

            cursor = connection.execute(
                """
                UPDATE challenges
                SET status = 'consumed'
                WHERE challenge_id = ? AND status = 'open'
                """,
                (fields["challenge_id"],),
            )
            if cursor.rowcount != 1:
                return VerificationResult(False, "Challenge already consumed")

        return VerificationResult(
            True,
            "Signature verified; execution disabled",
            execution_enabled=False,
        )
