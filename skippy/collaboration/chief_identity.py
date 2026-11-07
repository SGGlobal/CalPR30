"""
SKIPPY AI Collaboration Bridge — Offline Chief Identity Verifier.

SYNTHETIC. NON-EXECUTING.

A valid signature establishes possession of a signing key. It does not
establish informed human consent, and it does not enable execution.

Challenges originate from this verifier. A signature is accepted only when
it matches both the cryptographic payload and the trusted challenge scope.

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

    if len(fields["expected_sha"]) != SHA_LENGTH:
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
        not enroll an AI identity as Chief.
        """

        if not isinstance(key_id, str) or not key_id:
            raise IdentityError("key_id is required")
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise IdentityError("public key must be 32 bytes")

        Ed25519PublicKey.from_public_bytes(public_key)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
        now: datetime | None = None,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> dict:
        """
        Issue a trusted challenge bound to an exact authorization scope.

        The nonce is unpredictable. The challenge is persistent.
        """

        now = now or datetime.now(timezone.utc)
        if lifetime <= timedelta(0) or lifetime > MAX_CHALLENGE_LIFETIME:
            raise IdentityError("Invalid challenge lifetime")

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
            "key_id": "pending",
            "challenge_id": uuid.uuid4().hex,
            "nonce": uuid.uuid4().hex,
            "issued_at": now.isoformat(),
            "expires_at": (now + lifetime).isoformat(),
        }
        canonical_payload(challenge)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO challenges (
                    challenge_id, nonce, authorization_id, action,
                    action_digest, repository, environment, project_id,
                    task_id, expected_sha, issued_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
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

        Always returns execution_enabled=False.
        """

        now = now or datetime.now(timezone.utc)

        try:
            payload = canonical_payload(fields)
        except IdentityError as exc:
            return VerificationResult(False, str(exc))

        if not isinstance(signature, bytes) or len(signature) != 64:
            return VerificationResult(False, "Malformed signature")

        issued = datetime.fromisoformat(fields["issued_at"])
        expires = datetime.fromisoformat(fields["expires_at"])
        if issued > now:
            return VerificationResult(False, "Challenge issued in the future")
        if now >= expires:
            return VerificationResult(False, "Challenge expired")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            challenge = connection.execute(
                """
                SELECT authorization_id, action, action_digest, repository,
                       environment, project_id, task_id, expected_sha,
                       nonce, status
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
                nonce,
                status,
            ) = challenge

            if status != "open":
                return VerificationResult(False, "Challenge already consumed")

            scope = {
                "authorization_id": authorization_id,
                "action": action,
                "action_digest": action_digest,
                "repository": repository,
                "environment": environment,
                "project_id": project_id,
                "task_id": task_id,
                "expected_sha": expected_sha,
            }
            for name in SCOPE_FIELDS:
                if fields[name] != scope[name]:
                    return VerificationResult(
                        False, f"Scope mismatch: {name}"
                    )
            if fields["nonce"] != nonce:
                return VerificationResult(False, "Nonce does not match challenge")

            key = connection.execute(
                "SELECT public_key, status FROM trusted_keys WHERE key_id = ?",
                (fields["key_id"],),
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
