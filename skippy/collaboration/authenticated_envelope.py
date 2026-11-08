"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A.1 Authenticated Envelope Contract.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Defines the offline message envelope and validation rules for SKIPPY↔Jason
development handoff. External transport remains disabled.

SECURITY NOTE: payload_digest is an integrity checksum over the payload,
not cryptographic actor authentication. Trusted fixture injection is
modeled separately from untrusted inputs. No live secrets are introduced.
execution_enabled remains False.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class EnvelopeError(ValueError):
    """Raised when an envelope fails validation."""


SCHEMA_VERSION = "1.0"

# Explicit sender↔role↔event-type authorization matrix.
# Unknown combinations are rejected.
AUTHORIZATION_MATRIX: dict[tuple[str, str, str], str] = {
    ("chief", "chief", "assignment"): "ASSIGNMENT_CREATED",
    ("jason", "implementer", "implementation"): "IMPLEMENTATION_SUBMITTED",
    ("skippy", "reviewer", "review"): "REVIEW_RECORDED",
    ("skippy", "reviewer", "correction_request"): "CORRECTION_REQUESTED",
    ("jason", "implementer", "correction"): "CORRECTION_SUBMITTED",
}

ALLOWED_SENDERS = {"chief", "skippy", "jason"}
ALLOWED_ROLES = {"chief", "reviewer", "implementer"}
ALLOWED_RECIPIENTS = {"skippy", "jason", "chief", "bridge"}
MAX_CLOCK_SKEW_SECONDS = 60
MAX_ENVELOPE_AGE_SECONDS = 3600
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class AuthenticatedEnvelope:
    """Versioned message envelope for offline handoff.

    payload is stored as a frozen JSON string to prevent mutation after
    construction. The digest is an integrity checksum, not actor authentication.
    """

    project_id: str
    task_id: str
    checkpoint_id: str
    assignment_id: str
    message_id: str
    idempotency_key: str
    sender: str
    recipient: str
    role: str
    payload_json: str  # frozen canonical JSON
    payload_digest: str
    schema_version: str
    timestamp: datetime
    expiry: datetime
    sequence: int
    reviewed_sha: str | None = None
    # Trusted fixture flag: True only when the caller is a test fixture
    # that has already established Chief authorization outside this envelope.
    trusted_fixture: bool = False

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)

    def computed_digest(self) -> str:
        return hashlib.sha256(self.payload_json.encode()).hexdigest()


def build_envelope(
    *,
    project_id: str,
    task_id: str,
    checkpoint_id: str,
    assignment_id: str,
    message_id: str,
    idempotency_key: str,
    sender: str,
    recipient: str,
    role: str,
    payload: dict,
    timestamp: datetime,
    expiry: datetime,
    sequence: int,
    reviewed_sha: str | None = None,
    trusted_fixture: bool = False,
) -> AuthenticatedEnvelope:
    """Build an envelope with a computed payload digest."""
    if not isinstance(payload, dict):
        raise EnvelopeError("Payload must be a dict")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return AuthenticatedEnvelope(
        project_id=project_id,
        task_id=task_id,
        checkpoint_id=checkpoint_id,
        assignment_id=assignment_id,
        message_id=message_id,
        idempotency_key=idempotency_key,
        sender=sender,
        recipient=recipient,
        role=role,
        payload_json=canonical,
        payload_digest=digest,
        schema_version=SCHEMA_VERSION,
        timestamp=timestamp,
        expiry=expiry,
        sequence=sequence,
        reviewed_sha=reviewed_sha,
        trusted_fixture=trusted_fixture,
    )


def validate_envelope(
    envelope: AuthenticatedEnvelope,
    *,
    now: datetime,
    seen_message_ids: set[str],
    seen_idempotency_keys: set[str],
    expected_sequence: int | None = None,
) -> str:
    """
    Fail-closed validation of an envelope.

    Returns the mapped ledger event type on success.
    Raises EnvelopeError on any identity, digest, replay, ordering,
    expiry, or authorization failure.

    Note: payload_digest is an integrity checksum, not actor authentication.
    """
    if not isinstance(envelope.timestamp, datetime) or envelope.timestamp.tzinfo is None:
        raise EnvelopeError("Timestamp must be timezone-aware")
    if not isinstance(envelope.expiry, datetime) or envelope.expiry.tzinfo is None:
        raise EnvelopeError("Expiry must be timezone-aware")
    if envelope.expiry <= envelope.timestamp:
        raise EnvelopeError("Expiry must be after timestamp")
    if envelope.schema_version != SCHEMA_VERSION:
        raise EnvelopeError("Unsupported schema version")
    if envelope.sender not in ALLOWED_SENDERS:
        raise EnvelopeError("Unauthorized sender")
    if envelope.role not in ALLOWED_ROLES:
        raise EnvelopeError("Unauthorized role")
    if envelope.recipient not in ALLOWED_RECIPIENTS:
        raise EnvelopeError("Unauthorized recipient")
    if envelope.sender == envelope.recipient:
        raise EnvelopeError("Sender and recipient must differ")
    if not all(isinstance(x, str) and x for x in (
        envelope.project_id, envelope.task_id, envelope.checkpoint_id,
        envelope.assignment_id, envelope.message_id, envelope.idempotency_key,
    )):
        raise EnvelopeError("Required identity fields missing")
    if not isinstance(envelope.sequence, int) or envelope.sequence < 1:
        raise EnvelopeError("Sequence must be a positive integer")
    if envelope.computed_digest() != envelope.payload_digest:
        raise EnvelopeError("Payload digest mismatch")
    if envelope.message_id in seen_message_ids:
        raise EnvelopeError("Replay detected: message_id already seen")
    if envelope.idempotency_key in seen_idempotency_keys:
        raise EnvelopeError("Replay detected: idempotency_key already seen")
    age = (now - envelope.timestamp).total_seconds()
    if age > MAX_ENVELOPE_AGE_SECONDS:
        raise EnvelopeError("Envelope too old")
    if age < -MAX_CLOCK_SKEW_SECONDS:
        raise EnvelopeError("Envelope timestamp too far in the future")
    if now >= envelope.expiry:
        raise EnvelopeError("Envelope expired")
    if expected_sequence is not None and envelope.sequence != expected_sequence:
        raise EnvelopeError("Sequence mismatch")
    if envelope.reviewed_sha is not None and not SHA_PATTERN.match(envelope.reviewed_sha):
        raise EnvelopeError("Invalid reviewed SHA format")

    # Authorization matrix: exact sender/role/payload-type mapping required
    payload = envelope.payload
    payload_type = payload.get("type")
    if not isinstance(payload_type, str):
        raise EnvelopeError("Payload type missing or invalid")
    key = (envelope.sender, envelope.role, payload_type)
    if key not in AUTHORIZATION_MATRIX:
        raise EnvelopeError(f"Unauthorized sender/role/type combination: {key}")

    # Assignment requires trusted fixture or explicit external authorization
    if payload_type == "assignment" and not envelope.trusted_fixture:
        raise EnvelopeError("Assignment requires trusted fixture authorization")

    return AUTHORIZATION_MATRIX[key]
