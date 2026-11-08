"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A Authenticated Envelope Contract.

LOCAL SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Defines the offline authenticated message envelope and validation rules
for SKIPPY↔Jason development handoff. External transport remains disabled.
execution_enabled remains False.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class EnvelopeError(ValueError):
    """Raised when an envelope fails authentication or validation."""


SCHEMA_VERSION = "1.0"

ALLOWED_SENDERS = {"chief", "skippy", "jason"}
ALLOWED_ROLES = {"chief", "reviewer", "implementer"}
ALLOWED_RECIPIENTS = {"skippy", "jason", "chief", "bridge"}


@dataclass(frozen=True)
class AuthenticatedEnvelope:
    """Versioned authenticated message envelope for offline handoff."""

    project_id: str
    task_id: str
    checkpoint_id: str
    assignment_id: str
    message_id: str
    idempotency_key: str
    sender: str
    recipient: str
    role: str
    payload: dict
    payload_digest: str
    schema_version: str
    timestamp: datetime
    expiry: datetime
    sequence: int
    reviewed_sha: str | None = None
    chief_authorization: bool = False

    def canonical_payload(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))

    def computed_digest(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode()).hexdigest()


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
    chief_authorization: bool = False,
) -> AuthenticatedEnvelope:
    """Build an envelope with a computed payload digest."""
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
        payload=payload,
        payload_digest=digest,
        schema_version=SCHEMA_VERSION,
        timestamp=timestamp,
        expiry=expiry,
        sequence=sequence,
        reviewed_sha=reviewed_sha,
        chief_authorization=chief_authorization,
    )


def validate_envelope(
    envelope: AuthenticatedEnvelope,
    *,
    now: datetime,
    seen_message_ids: set[str],
    seen_idempotency_keys: set[str],
    expected_sequence: int | None = None,
) -> None:
    """
    Fail-closed validation of an authenticated envelope.

    Raises EnvelopeError on any identity, digest, replay, ordering,
    expiry, or authorization failure.
    """
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
    if envelope.computed_digest() != envelope.payload_digest:
        raise EnvelopeError("Payload digest mismatch")
    if envelope.message_id in seen_message_ids:
        raise EnvelopeError("Replay detected: message_id already seen")
    if envelope.idempotency_key in seen_idempotency_keys:
        raise EnvelopeError("Replay detected: idempotency_key already seen")
    if now >= envelope.expiry:
        raise EnvelopeError("Envelope expired")
    if envelope.timestamp > now:
        raise EnvelopeError("Envelope timestamp in the future")
    if expected_sequence is not None and envelope.sequence != expected_sequence:
        raise EnvelopeError("Sequence mismatch")
    # Assignment dispatch requires explicit Chief authorization
    if envelope.role == "chief" and envelope.payload.get("type") == "assignment":
        if not envelope.chief_authorization:
            raise EnvelopeError("Assignment dispatch requires explicit Chief authorization")
