"""
SKIPPY AI Collaboration Bridge — BRIDGE-02B.1 Offline Signature Verifier.

OFFLINE SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

This is a pure cryptographic identity verifier. It does not authorize,
admit, or enforce durable replay. A valid signature proves only that
a registered synthetic key signed the message.

execution_enabled remains False. External transport remains disabled.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

DOMAIN_PREFIX = b"QLA-BRIDGE-02B-v1\x00"  # exactly 18 bytes
MAX_AGE = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(seconds=60)
JS_SAFE_INT_MAX = 2**53 - 1


class SignatureError(Exception):
    """Fail-closed signature verification error."""


@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    actor_id: str
    permitted_role: str
    public_key: bytes  # 32 bytes
    state: str  # active | revoked | expired
    expires_at: datetime


@dataclass(frozen=True)
class TrustRoot:
    """Injected immutable synthetic trust root. Not a file."""
    version: int
    keys: tuple[KeyEntry, ...]


@dataclass(frozen=True)
class SignedEnvelope:
    schema_version: int
    sender: str
    recipient: str
    role: str
    project_id: str
    task_id: str
    checkpoint_id: str
    assignment_id: str
    message_id: str
    idempotency_key: str
    sequence: int
    timestamp: datetime
    expiry: datetime
    payload_digest: bytes  # 32 bytes
    payload: dict
    key_id: str
    signature: bytes  # 64 bytes


def _require_nfc(value: str, field: str) -> None:
    if not unicodedata.is_normalized("NFC", value):
        raise SignatureError(f"Non-NFC string in {field}")


def _canonical_json(payload: dict) -> bytes:
    """RFC 8785 constrained profile: sorted keys, no whitespace, integers only."""
    def check(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: check(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [check(v) for v in obj]
        if isinstance(obj, bool):
            raise SignatureError("Boolean not permitted in canonical JSON")
        if isinstance(obj, float):
            raise SignatureError("Float not permitted in canonical JSON")
        if isinstance(obj, int):
            if abs(obj) > JS_SAFE_INT_MAX:
                raise SignatureError("Integer exceeds JS-safe range")
            return obj
        if isinstance(obj, str):
            _require_nfc(obj, "payload")
            return obj
        raise SignatureError(f"Unsupported type in payload: {type(obj)}")

    checked = check(payload)
    return json.dumps(checked, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _encode_str(value: str) -> bytes:
    _require_nfc(value, "field")
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raise SignatureError("String exceeds uint16 length")
    return len(raw).to_bytes(2, "big") + raw


def _encode_timestamp(dt: datetime) -> bytes:
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise SignatureError("Timestamp must be UTC")
    text = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if len(text) != 20:
        raise SignatureError("Timestamp must be exactly 20 bytes")
    return text.encode("ascii")


def canonical_bytes(env: SignedEnvelope) -> bytes:
    """Produce the exact signed byte sequence."""
    payload_bytes = _canonical_json(env.payload)
    if len(payload_bytes) > 1_048_576:
        raise SignatureError("Payload exceeds 1 MiB")
    parts = [
        DOMAIN_PREFIX,
        bytes([env.schema_version]),
        _encode_str(env.sender),
        _encode_str(env.recipient),
        _encode_str(env.role),
        _encode_str(env.project_id),
        _encode_str(env.task_id),
        _encode_str(env.checkpoint_id),
        _encode_str(env.assignment_id),
        _encode_str(env.message_id),
        _encode_str(env.idempotency_key),
        env.sequence.to_bytes(8, "big"),
        _encode_timestamp(env.timestamp),
        _encode_timestamp(env.expiry),
        env.payload_digest,
        len(payload_bytes).to_bytes(4, "big") + payload_bytes,
        _encode_str(env.key_id),
    ]
    return b"".join(parts)


def load_trust_root(keys: list[KeyEntry], version: int) -> TrustRoot:
    """Validate and freeze a synthetic trust root. Fail closed on conflicts."""
    seen_ids: set[str] = set()
    actor_roles: dict[str, str] = {}
    for entry in keys:
        if entry.key_id in seen_ids:
            raise SignatureError(f"Duplicate key_id: {entry.key_id}")
        seen_ids.add(entry.key_id)
        if len(entry.public_key) != 32:
            raise SignatureError(f"Invalid public key length for {entry.key_id}")
        if entry.state not in ("active", "revoked", "expired"):
            raise SignatureError(f"Invalid state for {entry.key_id}")
        if entry.actor_id in actor_roles and actor_roles[entry.actor_id] != entry.permitted_role:
            raise SignatureError(f"Conflicting roles for actor {entry.actor_id}")
        actor_roles[entry.actor_id] = entry.permitted_role
    return TrustRoot(version=version, keys=tuple(keys))


def verify_signature(
    env: SignedEnvelope,
    trust_root: TrustRoot,
    now: datetime,
) -> str:
    """
    Pure cryptographic verification.

    Returns "cryptographically_valid" or raises SignatureError.
    Does not enforce durable replay. Does not authorize.
    """
    if env.schema_version != 1:
        raise SignatureError("Unsupported schema version")
    if "reviewed_sha" in env.payload:
        raise SignatureError("reviewed_sha not permitted in schema version 1")
    if len(env.signature) != 64:
        raise SignatureError("Signature must be exactly 64 bytes")
    if len(env.payload_digest) != 32:
        raise SignatureError("payload_digest must be exactly 32 bytes")

    # Time checks (exclusive boundaries)
    if now - env.timestamp >= MAX_AGE:
        raise SignatureError("Message exceeds maximum age")
    if env.timestamp - now >= MAX_FUTURE_SKEW:
        raise SignatureError("Message timestamp too far in the future")
    if now >= env.expiry:
        raise SignatureError("Message expired")

    # Registry lookup
    entry = next((k for k in trust_root.keys if k.key_id == env.key_id), None)
    if entry is None:
        raise SignatureError(f"Unknown key_id: {env.key_id}")
    if entry.state != "active":
        raise SignatureError(f"Key not active: {env.key_id}")
    if now >= entry.expires_at:
        raise SignatureError(f"Key expired: {env.key_id}")
    if entry.actor_id != env.sender:
        raise SignatureError("Actor mismatch")
    if entry.permitted_role != env.role:
        raise SignatureError("Role mismatch")

    # Signature verification
    try:
        signed = canonical_bytes(env)
        VerifyKey(entry.public_key).verify(signed, env.signature)
    except BadSignatureError as exc:
        raise SignatureError("Invalid signature") from exc
    except Exception as exc:
        raise SignatureError(f"Verification failed: {exc}") from exc

    return "cryptographically_valid"
