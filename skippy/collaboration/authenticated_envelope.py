"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A.2 Offline Integrity Contract.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

This is an offline integrity contract, not authenticated actor messaging.
payload_digest is an integrity checksum over the payload. Trusted fixture
injection is provided through a separate controlled dependency, not a claim
asserted by inbound envelope data. External transport remains disabled.
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

# Explicit sender↔role↔recipient↔event-type authorization matrix.
# Unknown combinations are rejected.
AUTHORIZATION_MATRIX: dict[tuple[str, str, str, str], str] = {
    ("chief", "chief", "jason", "assignment"): "ASSIGNMENT_CREATED",
    ("jason", "implementer", "skippy", "implementation"): "IMPLEMENTATION_SUBMITTED",
    ("skippy", "reviewer", "jason", "review"): "REVIEW_RECORDED",
    ("skippy", "reviewer", "jason", "correction_request"): "CORRECTION_REQUESTED",
    ("jason", "implementer", "skippy", "correction"): "CORRECTION_SUBMITTED",
}

ALLOWED_SENDERS = {"chief", "skippy", "jason"}
ALLOWED_ROLES = {"chief", "reviewer", "implementer"}
ALLOWED_RECIPIENTS = {"skippy", "jason", "chief", "bridge"}
MAX_CLOCK_SKEW_SECONDS = 60
MAX_ENVELOPE_AGE_SECONDS = 3600
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Required payload fields per event type
REQUIRED_PAYLOAD_FIELDS: dict[str, set[str]] = {
    "assignment": {"objective"},
    "implementation": {"sha"},
    "review": {"sha", "source_verified"},
    "correction_request": {"findings"},
    "correction": {"sha"},
}


@dataclass(frozen=True)
class AuthenticatedEnvelope:
    """Versioned message envelope for offline integrity contract.

    payload is stored as a frozen JSON string. The digest is an integrity
    checksum, not actor authentication. trusted_fixture is not an envelope
    field; it is supplied separately by the adapter.
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
    payload_json: str
    payload_digest: str
    schema_version: str
    timestamp: datetime
    expiry: datetime
    sequence: int
    reviewed_sha: str | None = None

    @property
    def payload(self) -> dict:
        try:
            data = json.loads(self.payload_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise EnvelopeError(f"Malformed payload JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise EnvelopeError("Payload must be a JSON object")
        return data

    def computed_digest(self) -> str:
        try:
            return hashlib.sha256(self.payload_json.encode()).hexdigest()
        except (AttributeError, TypeError) as exc:
            raise EnvelopeError(f"Cannot compute digest: {exc}") from exc


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
    )


def validate_envelope(
    envelope: AuthenticatedEnvelope,
    *,
    now: datetime,
    seen_message_ids: set[str],
    seen_idempotency_keys: set[str],
    expected_sequence: int,
) -> str:
    """
    Fail-closed validation of an envelope.

    Returns the mapped ledger event type on success.
    Raises EnvelopeError on any identity, digest, replay, ordering,
    expiry, or authorization failure.

    Note: payload_digest is an integrity checksum, not actor authentication.
    expected_sequence is required (not optional).
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise EnvelopeError("now must be a timezone-aware datetime")
    if not isinstance(envelope.timestamp, datetime) or envelope.timestamp.tzinfo is None:
        raise EnvelopeError("Timestamp must be timezone-aware")
    if not isinstance(envelope.expiry, datetime) or envelope.expiry.tzinfo is None:
        raise EnvelopeError("Expiry must be timezone-aware")
    if envelope.expiry <= envelope.timestamp:
        raise EnvelopeError("Expiry must be after timestamp")
    if envelope.schema_version != SCHEMA_VERSION:
        raise EnvelopeError("Unsupported schema version")
    if not isinstance(envelope.sender, str) or envelope.sender not in ALLOWED_SENDERS:
        raise EnvelopeError("Unauthorized sender")
    if not isinstance(envelope.role, str) or envelope.role not in ALLOWED_ROLES:
        raise EnvelopeError("Unauthorized role")
    if not isinstance(envelope.recipient, str) or envelope.recipient not in ALLOWED_RECIPIENTS:
        raise EnvelopeError("Unauthorized recipient")
    if envelope.sender == envelope.recipient:
        raise EnvelopeError("Sender and recipient must differ")
    if not all(isinstance(x, str) and x for x in (
        envelope.project_id, envelope.task_id, envelope.checkpoint_id,
        envelope.assignment_id, envelope.message_id, envelope.idempotency_key,
    )):
        raise EnvelopeError("Required identity fields missing")
    if not isinstance(envelope.sequence, int) or isinstance(envelope.sequence, bool) or envelope.sequence < 1:
        raise EnvelopeError("Sequence must be a positive integer")
    try:
        if envelope.computed_digest() != envelope.payload_digest:
            raise EnvelopeError("Payload digest mismatch")
    except EnvelopeError:
        raise
    except Exception as exc:
        raise EnvelopeError(f"Digest validation failed: {exc}") from exc
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
    if envelope.sequence != expected_sequence:
        raise EnvelopeError("Sequence mismatch")
    if envelope.reviewed_sha is not None and not SHA_PATTERN.match(envelope.reviewed_sha):
        raise EnvelopeError("Invalid reviewed SHA format")

    # Authorization matrix including recipient
    try:
        payload = envelope.payload
    except EnvelopeError:
        raise
    except Exception as exc:
        raise EnvelopeError(f"Payload validation failed: {exc}") from exc
    payload_type = payload.get("type")
    if not isinstance(payload_type, str):
        raise EnvelopeError("Payload type missing or invalid")
    key = (envelope.sender, envelope.role, envelope.recipient, payload_type)
    if key not in AUTHORIZATION_MATRIX:
        raise EnvelopeError(f"Unauthorized sender/role/recipient/type combination: {key}")

    # Required payload fields
    required = REQUIRED_PAYLOAD_FIELDS.get(payload_type, set())
    if not required.issubset(payload.keys()):
        raise EnvelopeError(f"Missing required payload fields: {required - payload.keys()}")

    return AUTHORIZATION_MATRIX[key]
