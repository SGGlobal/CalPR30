"""
SKIPPY AI Collaboration Bridge — Offline Chief Identity Verifier.

SYNTHETIC. NON-EXECUTING.

A valid signature establishes possession of a signing key. It does not
establish informed human consent, and it does not enable execution.

The verifier does not provide a general-purpose signing capability.
Private keys are never persisted by this module.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


PROTOCOL_VERSION = "chief-auth-1"
DOMAIN = "skippy.chief.authorization"
APPLICATION = "skippy-ai-collaboration-bridge"


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

    if fields["application"] != APPLICATION:
        raise IdentityError("Unexpected application")
    if fields["protocol_version"] != PROTOCOL_VERSION:
        raise IdentityError("Unsupported protocol version")

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
    Offline signature verifier.

    Accepts a trusted public key through an isolated configuration.
    Does not sign. Does not enable execution.
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
                CREATE TABLE IF NOT EXISTS used_nonces (
                    nonce TEXT PRIMARY KEY,
                    challenge_id TEXT NOT NULL,
                    accepted_at TEXT NOT NULL
                )
                """
            )

    def register_public_key(self, key_id: str, public_key: bytes) -> None:
        """
        Register a trusted public key.

        This is a test-facing configuration method. It does not accept a
        private key and does not enroll an AI identity as Chief.
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

    def verify(
        self,
        fields: dict,
        signature: bytes | None,
        now: datetime | None = None,
    ) -> VerificationResult:
        """
        Verify a signature over the exact canonical payload.

        Always returns execution_enabled=False.
        """

        now = now or datetime.now(timezone.utc)

        try:
            payload = canonical_payload(fields)
        except IdentityError as exc:
            return VerificationResult(False, str(exc))

        if not isinstance(signature, bytes) or len(signature) != 64:
            return VerificationResult(False, "Malformed signature")

        expires = datetime.fromisoformat(fields["expires_at"])
        if expires.tzinfo is None or now >= expires:
            return VerificationResult(False, "Challenge expired")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            row = connection.execute(
                "SELECT public_key, status FROM trusted_keys WHERE key_id = ?",
                (fields["key_id"],),
            ).fetchone()

            if row is None:
                return VerificationResult(False, "Unknown public key")

            public_bytes, status = row
            if status != "active":
                return VerificationResult(False, "Key revoked")

            existing = connection.execute(
                "SELECT nonce FROM used_nonces WHERE nonce = ?",
                (fields["nonce"],),
            ).fetchone()
            if existing is not None:
                return VerificationResult(False, "Nonce already used")

            public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
            try:
                public_key.verify(signature, payload)
            except InvalidSignature:
                return VerificationResult(False, "Invalid signature")

            connection.execute(
                """
                INSERT INTO used_nonces (nonce, challenge_id, accepted_at)
                VALUES (?, ?, ?)
                """,
                (
                    fields["nonce"],
                    fields["challenge_id"],
                    now.isoformat(),
                ),
            )

        return VerificationResult(
            True,
            "Signature verified; execution disabled",
            execution_enabled=False,
        )


def new_nonce() -> str:
    """Unpredictable nonce for tests. Not a signing capability."""

    return uuid.uuid4().hex
