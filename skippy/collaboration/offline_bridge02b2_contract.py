"""BRIDGE-02B.2.2: pure, synthetic-only workflow contract.

This module grants no authorization and performs no I/O. A workflow snapshot is
an ephemeral test oracle, never a durable replay or operational admission gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta


class SyntheticContractError(ValueError):
    """Input or simulated transition violates the closed test contract."""


MATRIX = {
    "assignment": ("chief", "chief", "jason"),
    "implementation": ("jason", "implementer", "skippy"),
    "correction": ("jason", "implementer", "skippy"),
    "review": ("skippy", "reviewer", "jason"),
}
FIELDS = {
    "assignment": frozenset({"type", "objective", "evidence_id", "base_sha"}),
    "implementation": frozenset({"type", "evidence_id", "implementation_sha"}),
    "correction": frozenset({"type", "evidence_id", "implementation_sha"}),
    "review": frozenset({"type", "evidence_id", "implementation_sha", "verdict"}),
}
VERDICTS = frozenset({"approved", "changes_requested"})
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SyntheticInput:
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
    payload: dict


@dataclass(frozen=True)
class WorkflowSnapshot:
    """One stream only. Empty on first use; never reconstructed from SQLite."""

    stage: str = "EMPTY"
    project_id: str | None = None
    task_id: str | None = None
    checkpoint_id: str | None = None
    assignment_id: str | None = None
    next_sequence: int = 1
    latest_sha: str | None = None
    used_shas: tuple[str, ...] = ()
    # Each item is (message_id, idempotency_key, complete input fingerprint).
    receipts: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class TransitionPlan:
    message: SyntheticInput
    next_snapshot: WorkflowSnapshot
    duplicate_candidate: bool = False


def _valid_id(value: object) -> bool:
    return type(value) is str and bool(value.strip()) and value == value.strip()


def _valid_sha(value: object) -> bool:
    return type(value) is str and SHA_PATTERN.fullmatch(value) is not None


def _freeze_message(message: SyntheticInput) -> SyntheticInput:
    if type(message) is not SyntheticInput or type(message.payload) is not dict:
        raise SyntheticContractError("only exact synthetic input and plain payload permitted")
    # One scalar-only defensive copy: no caller-owned payload object is used again.
    try:
        payload = dict(message.payload)
        if any(type(k) is not str for k in payload):
            raise ValueError("non-string key")
        if any(type(v) is not str for v in payload.values()):
            raise ValueError("non-string value")
        return replace(message, payload=payload)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise SyntheticContractError("malformed synthetic payload") from exc


def _fingerprint(message: SyntheticInput) -> str:
    values = {
        "sender": message.sender, "recipient": message.recipient, "role": message.role,
        "project_id": message.project_id, "task_id": message.task_id,
        "checkpoint_id": message.checkpoint_id, "assignment_id": message.assignment_id,
        "message_id": message.message_id, "idempotency_key": message.idempotency_key,
        "sequence": message.sequence, "timestamp": message.timestamp.isoformat(),
        "expiry": message.expiry.isoformat(), "payload": message.payload,
    }
    data = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def policy_inspection_copy(message: SyntheticInput) -> SyntheticInput:
    """Scalar-only copy for fixture policy. Never alias the internal validated payload."""
    if type(message) is not SyntheticInput or type(message.payload) is not dict:
        raise SyntheticContractError("only exact synthetic input and plain payload permitted")
    try:
        payload = dict(message.payload)
        if any(type(k) is not str for k in payload) or any(type(v) is not str for v in payload.values()):
            raise ValueError("non-scalar payload")
    except (TypeError, ValueError, RuntimeError) as exc:
        raise SyntheticContractError("malformed synthetic payload") from exc
    copy = replace(message, payload=payload)
    if copy.payload is message.payload:
        raise SyntheticContractError("policy copy must not alias validated payload")
    return copy


def plan_transition(message: SyntheticInput, snapshot: WorkflowSnapshot) -> TransitionPlan:
    """Classify a *simulation* transition. Always require B2.1 gate validation later."""
    if type(snapshot) is not WorkflowSnapshot:
        raise SyntheticContractError("invalid synthetic workflow snapshot")
    msg = _freeze_message(message)
    for field in ("sender", "recipient", "role", "project_id", "task_id",
                  "checkpoint_id", "assignment_id", "message_id", "idempotency_key"):
        if not _valid_id(getattr(msg, field)):
            raise SyntheticContractError(f"invalid {field}")
    if type(msg.sequence) is not int or not 1 <= msg.sequence < 2**64:
        raise SyntheticContractError("invalid sequence")
    for field in ("timestamp", "expiry"):
        value = getattr(msg, field)
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise SyntheticContractError(f"{field} must be UTC-aware")
    if msg.expiry <= msg.timestamp:
        raise SyntheticContractError("invalid expiry")
    kind = msg.payload.get("type")
    if type(kind) is not str or kind not in MATRIX:
        raise SyntheticContractError("unsupported synthetic event type")
    if frozenset(msg.payload) != FIELDS[kind]:
        raise SyntheticContractError("unknown or missing signed payload fields")
    if (msg.sender, msg.role, msg.recipient) != MATRIX[kind]:
        raise SyntheticContractError("unauthorized sender/role/recipient/type")
    if not _valid_id(msg.payload["evidence_id"]):
        raise SyntheticContractError("invalid evidence_id")
    sha_field = "base_sha" if kind == "assignment" else "implementation_sha"
    if not _valid_sha(msg.payload[sha_field]):
        raise SyntheticContractError(f"invalid {sha_field}")
    if kind == "assignment" and not _valid_id(msg.payload["objective"]):
        raise SyntheticContractError("invalid objective")
    if kind == "review" and msg.payload["verdict"] not in VERDICTS:
        raise SyntheticContractError("invalid review verdict")

    fingerprint = _fingerprint(msg)
    if snapshot.stage != "EMPTY":
        if (msg.project_id, msg.task_id, msg.checkpoint_id, msg.assignment_id) != (
            snapshot.project_id, snapshot.task_id, snapshot.checkpoint_id, snapshot.assignment_id
        ):
            raise SyntheticContractError("stream or assignment binding mismatch")
    # One message/idempotency identity namespace per task; this simulator
    # intentionally only supports one task stream per instance.
    matching = [r for r in snapshot.receipts
                if r[0] == msg.message_id or r[1] == msg.idempotency_key]
    if matching:
        if len(matching) == 1 and matching[0] == (msg.message_id, msg.idempotency_key, fingerprint):
            return TransitionPlan(msg, snapshot, duplicate_candidate=True)
        raise SyntheticContractError("conflicting identity reuse")

    stage = snapshot.stage
    if stage == "EMPTY" and kind == "assignment":
        next_stage = "AWAIT_IMPLEMENTATION"
        next_sha = None
        used = (msg.payload["base_sha"],)
    elif stage == "AWAIT_IMPLEMENTATION" and kind == "implementation":
        new_sha = msg.payload["implementation_sha"]
        if new_sha in snapshot.used_shas:
            raise SyntheticContractError("implementation SHA reused")
        next_stage, next_sha, used = "AWAIT_REVIEW", new_sha, snapshot.used_shas + (new_sha,)
    elif stage == "AWAIT_REVIEW" and kind == "review":
        if msg.payload["implementation_sha"] != snapshot.latest_sha:
            raise SyntheticContractError("review SHA does not match latest implementation")
        next_stage = "CLOSED" if msg.payload["verdict"] == "approved" else "AWAIT_CORRECTION"
        next_sha, used = snapshot.latest_sha, snapshot.used_shas
    elif stage == "AWAIT_CORRECTION" and kind == "correction":
        new_sha = msg.payload["implementation_sha"]
        if new_sha in snapshot.used_shas:
            raise SyntheticContractError("correction SHA must be new")
        next_stage, next_sha, used = "AWAIT_REVIEW", new_sha, snapshot.used_shas + (new_sha,)
    else:
        raise SyntheticContractError("invalid workflow transition")
    if msg.sequence != snapshot.next_sequence:
        raise SyntheticContractError(f"sequence {msg.sequence} != {snapshot.next_sequence}")
    new_snapshot = replace(
        snapshot, stage=next_stage, project_id=msg.project_id,
        task_id=msg.task_id, checkpoint_id=msg.checkpoint_id,
        assignment_id=msg.assignment_id, next_sequence=msg.sequence + 1,
        latest_sha=next_sha, used_shas=used,
        receipts=snapshot.receipts + ((msg.message_id, msg.idempotency_key, fingerprint),),
    )
    return TransitionPlan(msg, new_snapshot)
