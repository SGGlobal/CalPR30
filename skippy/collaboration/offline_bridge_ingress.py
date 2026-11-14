"""
SKIPPY AI Collaboration Bridge - BRIDGE-02B.2.2 offline signed-frame ingress.

OFFLINE SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED. PROTOTYPE.

Work order https://github.com/SGGlobal/QLA/issues/23#issuecomment-6872880664
implementing J12.2 of docs/bridge-02b2-compatibility-contract.md (as corrected
by PR #26 review 6872447073).

One call, ``deliver(frame, adapter, sink, now)``:

1. a strict decoder turns one byte frame into a frozen ``SignedEnvelope``
   (decoding authenticates nothing; S4 inside the adapter remains the
   authentication point);
2. the caller-supplied, already-opened signed adapter's existing
   ``accept_signed`` is invoked at most once;
3. one ``AckRecord`` is handed at most once to an injected recording sink.

Admission and acknowledgment are independent dimensions; a sink outcome never
changes the admission result. This module adds no authorization, evidence,
HALT, replay, retry, resend, lookup, re-acknowledgment or recovery logic. It
never opens, creates or provisions a store, never loads keys or credentials,
and has no network, GitHub transport or runner path. ``ADMITTED`` means only
"authenticated and durably admitted by this offline prototype"; it is not
source review, implementation completion, CI, Chief approval or permission to
execute work. execution_enabled remains False.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from skippy.collaboration.offline_authenticated_adapter import SignedAdmissionError
from skippy.collaboration.offline_signature_verifier import SignedEnvelope

execution_enabled = False

MAX_FRAME_BYTES = 65536
MAX_DEPTH = 32
ACK_RECORD_VERSION = 1

ENVELOPE_FIELDS = (
    "schema_version", "sender", "recipient", "role", "project_id", "task_id",
    "checkpoint_id", "assignment_id", "message_id", "idempotency_key", "sequence",
    "timestamp", "expiry", "payload_digest", "payload", "key_id", "signature",
)
_INT_FIELDS = ("schema_version", "sequence")
_STR_FIELDS = (
    "sender", "recipient", "role", "project_id", "task_id", "checkpoint_id",
    "assignment_id", "message_id", "idempotency_key", "key_id",
)
_HEX_FIELDS = {"payload_digest": 64, "signature": 128}
_TIME_FIELDS = ("timestamp", "expiry")
_TIMESTAMP = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_HEX = {64: re.compile(r"[0-9a-f]{64}"), 128: re.compile(r"[0-9a-f]{128}")}
_BOM = b"\xef\xbb\xbf"

_J2_CLASSIFICATIONS = frozenset({"refused", "rejected", "stop_retained", "confirmed_rolled_back", "ambiguous"})
_PRE_ARM_REFUSAL_STEPS = frozenset({"S1", "S2", "T1"})


class Admission(str, Enum):
    MALFORMED = "MALFORMED"
    ADMITTED = "ADMITTED"
    REFUSED = "REFUSED"
    REJECTED = "REJECTED"
    STOP_RETAINED = "STOP_RETAINED"
    UNCERTAIN = "UNCERTAIN"


class Ack(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    CONFIRMED_BY_SINK = "CONFIRMED_BY_SINK"
    NOT_CONFIRMED = "NOT_CONFIRMED"


_VERIFIED_ADMISSIONS = frozenset({Admission.ADMITTED, Admission.REJECTED, Admission.STOP_RETAINED})


@dataclass(frozen=True)
class Correlation:
    """Decoded identifiers: correlation only, never sender authentication."""
    message_id: str
    idempotency_key: str
    sequence: int


@dataclass(frozen=True)
class AckRecord:
    """The single record handed to the sink. Carries no sender, role or key_id."""
    record_version: int
    admission: Admission
    adapter_step: str | None
    correlation: Correlation | None
    correlation_verified: bool


@dataclass(frozen=True)
class IngressResult:
    admission: Admission
    adapter_step: str | None
    adapter_classification: str | None
    poisoned: bool | None
    adapter_calls: int
    correlation: Correlation | None
    correlation_verified: bool
    ack: Ack
    detail: str


class MalformedFrame(ValueError):
    """Decoder refusal. Raised before any adapter or sink call."""


# --- decoder -----------------------------------------------------------------


def _check_depth(text: str) -> None:
    """Container depth pre-scan outside string literals (top-level object = 1)."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > MAX_DEPTH:
                raise MalformedFrame(f"nesting depth exceeds {MAX_DEPTH}")
        elif ch in "}]":
            depth -= 1


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MalformedFrame("duplicate object key")
        result[key] = value
    return result


def _reject_constant(name):
    raise MalformedFrame(f"non-finite number {name}")


def _reject_float(text):
    raise MalformedFrame("non-integer number")


def _check_text(value: str) -> None:
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise MalformedFrame("lone surrogate in string")


def _check_strings(obj) -> None:
    if isinstance(obj, str):
        _check_text(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _check_text(key)
            _check_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            _check_strings(value)


def _timestamp(value) -> datetime:
    if type(value) is not str:
        raise MalformedFrame("timestamp must be a string")
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise MalformedFrame("timestamp grammar")
    try:
        parsed = datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise MalformedFrame("timestamp value") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise MalformedFrame("timestamp does not round-trip")
    return parsed


def decode_signed_frame(frame: bytes) -> SignedEnvelope:
    """Strict J12.2.1 profile. Raises MalformedFrame; never repairs signed values."""
    if type(frame) is not bytes:
        raise MalformedFrame("frame must be bytes")
    if len(frame) > MAX_FRAME_BYTES:
        raise MalformedFrame(f"frame exceeds {MAX_FRAME_BYTES} bytes")
    if frame.startswith(_BOM):
        raise MalformedFrame("byte order mark")
    try:
        text = frame.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise MalformedFrame("invalid UTF-8") from None
    _check_depth(text)
    try:
        obj = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant,
                         parse_float=_reject_float)
    except MalformedFrame:
        raise
    except (ValueError, RecursionError) as exc:
        raise MalformedFrame(f"invalid JSON: {type(exc).__name__}") from None
    _check_strings(obj)
    if type(obj) is not dict:
        raise MalformedFrame("frame must be a JSON object")
    if set(obj) != set(ENVELOPE_FIELDS):
        raise MalformedFrame("field set must be exactly the SignedEnvelope fields")
    values = {}
    for name in _INT_FIELDS:
        if type(obj[name]) is not int:
            raise MalformedFrame(f"{name} must be an integer")
        values[name] = obj[name]
    for name in _STR_FIELDS:
        if type(obj[name]) is not str:
            raise MalformedFrame(f"{name} must be a string")
        values[name] = obj[name]
    for name, length in _HEX_FIELDS.items():
        value = obj[name]
        if type(value) is not str or _HEX[length].fullmatch(value) is None:
            raise MalformedFrame(f"{name} must be exactly {length} lowercase hex characters")
        values[name] = bytes.fromhex(value)
    for name in _TIME_FIELDS:
        values[name] = _timestamp(obj[name])
    if type(obj["payload"]) is not dict:
        raise MalformedFrame("payload must be a JSON object")
    values["payload"] = obj["payload"]
    return SignedEnvelope(**values)


# --- outcome mapping -----------------------------------------------------------


def _map_error(exc: SignedAdmissionError):
    raw_step = getattr(exc, "step", None)
    raw = getattr(exc, "classification", None)
    raw_poison = getattr(exc, "poisoned", None)
    step = raw_step if type(raw_step) is str else None
    classification = raw if type(raw) is str and raw in _J2_CLASSIFICATIONS else None
    poisoned = raw_poison if type(raw_poison) is bool else None
    if classification == "refused" and step in _PRE_ARM_REFUSAL_STEPS:
        admission = Admission.REFUSED
    elif classification == "rejected" and step in ("E1.b", "S10"):
        admission = Admission.REJECTED
    elif classification == "stop_retained" and step == "S9":
        admission = Admission.STOP_RETAINED
    else:
        # Post-arm owner refusals, confirmed_rolled_back, ambiguous and any
        # unknown classification or step: never a definite no-effect result.
        admission = Admission.UNCERTAIN
    detail = f"SignedAdmissionError classification={raw!r} step={raw_step!r}"
    return admission, step, classification, poisoned, detail


def deliver(frame: bytes, adapter, sink, now) -> IngressResult:
    """Decode once, call ``adapter.accept_signed`` at most once, call ``sink.record`` at most once.

    ``adapter`` is a signed adapter the caller already opened; ``now`` is passed
    to it unchanged. Ordinary ``Exception`` from the adapter maps to UNCERTAIN
    and from the sink to NOT_CONFIRMED; ``BaseException`` (interrupts) is not
    caught. There is no retry, resend, lookup or recovery path.
    """
    try:
        envelope = decode_signed_frame(frame)
    except MalformedFrame as exc:
        return IngressResult(
            admission=Admission.MALFORMED, adapter_step=None, adapter_classification=None,
            poisoned=None, adapter_calls=0, correlation=None, correlation_verified=False,
            ack=Ack.NOT_ATTEMPTED, detail=f"malformed: {exc}",
        )
    correlation = Correlation(envelope.message_id, envelope.idempotency_key, envelope.sequence)
    try:
        returned = adapter.accept_signed(envelope, now=now)
    except SignedAdmissionError as exc:
        admission, step, classification, poisoned, detail = _map_error(exc)
    except Exception as exc:  # noqa: BLE001 - unknown adapter outcome stays UNCERTAIN
        admission, step, classification, poisoned = Admission.UNCERTAIN, None, None, None
        detail = f"unexpected adapter exception {type(exc).__name__}"
    else:
        step, poisoned = None, None
        if type(returned) is str and returned == "accepted":
            admission, classification, detail = Admission.ADMITTED, "accepted", "accepted"
        else:
            admission, classification = Admission.UNCERTAIN, None
            detail = f"unexpected adapter return of type {type(returned).__name__}"
    verified = admission in _VERIFIED_ADMISSIONS
    record = AckRecord(
        record_version=ACK_RECORD_VERSION, admission=admission, adapter_step=step,
        correlation=correlation, correlation_verified=verified,
    )
    try:
        sink.record(record)
    except Exception as exc:  # noqa: BLE001 - delivery not confirmed, never proof of absence
        ack = Ack.NOT_CONFIRMED
        detail = f"{detail}; sink raised {type(exc).__name__}"
    else:
        ack = Ack.CONFIRMED_BY_SINK
    return IngressResult(
        admission=admission, adapter_step=step, adapter_classification=classification,
        poisoned=poisoned, adapter_calls=1, correlation=correlation, correlation_verified=verified,
        ack=ack, detail=detail,
    )
