"""BRIDGE-02B.2.2 offline signed-frame ingress tests (work order 6872880664).

OFFLINE SYNTHETIC ONLY. Governing acceptance: J12.3 cases 1-8 of
docs/bridge-02b2-compatibility-contract.md as corrected by PR #26 review
6872447073 and the work order's controlling rulings.

Every store is a fresh tmp_path SQLite set provisioned here (receipts through
``provision_signed_receipts_store``); keys are deterministic test seeds with
'test-b2b2-ingress-' key IDs; trust, evidence and HALT are injected fixtures.
The real ``open_signed`` adapter, gate, ledger and verifier are used unchanged.

Labelled test seams (test-local; never production code):
- CALL COUNTER: an instance attribute wrapping ``adapter.accept_signed`` that
  only counts and delegates;
- RECORDING SINK: modes normal / raise_before / record_then_raise / interrupt,
  observing durable state at sink entry;
- UNKNOWN-ADAPTER DOUBLE: an object with ``accept_signed`` producing outcomes
  the real adapter does not, for mapping checks only;
- WHOLE-CALL gate.reserve / ledger.record_event wrappers and a receipts
  connection wrapper on the private ``_receipts_connect`` factory (the
  existing P-6 seam). Internal gate/ledger commit seams are not faulted.

Classifications asserted are the caller's own observation (J2); fresh reads
describe actual effects and never upgrade caller knowledge. Counts are not a
claim of exhaustive security. execution_enabled=False; no network,
credentials, GitHub transport or runner.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

import skippy.collaboration.offline_bridge_ingress as ingress
from skippy.collaboration.checkpoint_ledger import CheckpointLedger
from skippy.collaboration.offline_admission_gate import OfflineAdmissionGate
from skippy.collaboration.offline_authenticated_adapter import (
    OfflineAuthenticatedAdapter,
    SignedAdmissionError,
    provision_signed_receipts_store,
)
from skippy.collaboration.offline_bridge_ingress import (
    Ack,
    AckRecord,
    Admission,
    Correlation,
    IngressResult,
    MalformedFrame,
    decode_signed_frame,
    deliver,
)
from skippy.collaboration.offline_signature_verifier import (
    KeyEntry,
    SignedEnvelope,
    _canonical_json,
    canonical_bytes,
    load_trust_root,
    verify_signature,
)

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
IMPL_SHA = "fedcba9876543210fedcba9876543210fedcba98"
PROJECT, TASK, CHECKPOINT, ASSIGNMENT = "QLA", "task-ingress", "ckpt-ingress", "assign-ingress"
KINDS = {
    "assignment": ("chief", "chief", "jason",
                   {"type": "assignment", "objective": "synthetic ingress", "evidence_id": "fixture-assign",
                    "base_sha": BASE_SHA}),
    "implementation": ("jason", "implementer", "skippy",
                       {"type": "implementation", "evidence_id": "fixture-impl", "implementation_sha": IMPL_SHA}),
    "review": ("skippy", "reviewer", "jason",
               {"type": "review", "evidence_id": "fixture-review", "implementation_sha": IMPL_SHA,
                "verdict": "approved"}),
}
SEQUENCE = {"assignment": 1, "implementation": 2, "review": 3}
REPLAY = "replay: identity already admitted"


class InjectedFault(Exception):
    """Test-local injected failure (never raised by production code)."""


class _Interrupt(BaseException):
    """Test-local process-interrupt stand-in (not an ordinary Exception)."""


# --- synthetic fixtures --------------------------------------------------------


def make_keys():
    actors = {"chief": ("chief", b"\x71" * 32), "jason": ("implementer", b"\x72" * 32),
              "skippy": ("reviewer", b"\x73" * 32)}
    signers, entries = {}, []
    for actor, (role, seed) in actors.items():
        key_id = "test-b2b2-ingress-" + actor
        signer = SigningKey(seed)
        signers[actor] = (key_id, signer)
        entries.append(KeyEntry(key_id, actor, role, signer.verify_key.encode(), "active",
                                NOW + timedelta(days=30)))
    return load_trust_root(entries, version=1), signers


class FixtureEvidence:
    def __init__(self, verdict="verified"):
        self.verdict = verdict

    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        if not evidence_id.startswith("fixture-") or assignment_id != ASSIGNMENT:
            return "rejected"
        return self.verdict


class FixtureProbe:
    def __init__(self):
        self.state = "clear"

    def read(self):
        return self.state


class HaltCheck:
    """Per-call scripted adapter HALT dependency: 'clear', 'halted' or 'raise'."""

    def __init__(self, states=("clear",)):
        self.states = tuple(states)
        self.calls = 0

    def __call__(self):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        if state == "raise":
            raise RuntimeError("synthetic HALT probe unavailable")
        return state == "halted"


def make_envelope(signers, kind="assignment", tag="a", *, sequence=None, recipient=None,
                  payload_set=None, mid=None, idem=None, sign_as=None, key=None,
                  bad_signature=False) -> SignedEnvelope:
    sender, role, default_recipient, payload = KINDS[kind]
    payload = dict(payload)
    payload.update(payload_set or {})
    key_id, signer = key if key is not None else signers[sign_as or sender]
    unsigned = SignedEnvelope(
        schema_version=1, sender=sender, recipient=recipient or default_recipient, role=role,
        project_id=PROJECT, task_id=TASK, checkpoint_id=CHECKPOINT, assignment_id=ASSIGNMENT,
        message_id=mid or f"ing-{kind}-{tag}", idempotency_key=idem or f"ing-idem-{kind}-{tag}",
        sequence=SEQUENCE[kind] if sequence is None else sequence,
        timestamp=NOW - timedelta(seconds=60), expiry=NOW + timedelta(hours=1),
        payload_digest=hashlib.sha256(_canonical_json(payload)).digest(), payload=payload,
        key_id=key_id, signature=b"\x00" * 64,
    )
    signature = signer.sign(canonical_bytes(unsigned)).signature
    if bad_signature:
        signature = bytes([signature[0] ^ 0x01]) + signature[1:]
    return replace(unsigned, signature=signature)


def frame_obj(env: SignedEnvelope) -> dict:
    """Test-only encoder input: the J12.2.1 wire shape of one envelope."""
    return {
        "schema_version": env.schema_version, "sender": env.sender, "recipient": env.recipient,
        "role": env.role, "project_id": env.project_id, "task_id": env.task_id,
        "checkpoint_id": env.checkpoint_id, "assignment_id": env.assignment_id,
        "message_id": env.message_id, "idempotency_key": env.idempotency_key,
        "sequence": env.sequence, "timestamp": env.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry": env.expiry.strftime("%Y-%m-%dT%H:%M:%SZ"), "payload_digest": env.payload_digest.hex(),
        "payload": env.payload, "key_id": env.key_id, "signature": env.signature.hex(),
    }


def dump(obj, *, ensure_ascii=False) -> bytes:
    return json.dumps(obj, ensure_ascii=ensure_ascii, separators=(",", ":")).encode("utf-8")


def to_frame(env, **kwargs) -> bytes:
    return dump(frame_obj(env), **kwargs)


class Stores:
    """One fresh disposable store set (receipts, admission, ledger) built by the test only."""

    def __init__(self, root):
        self.root = Path(root)
        self.receipts = self.root / "receipts.db"
        self.admission = self.root / "admission.db"
        self.ledger_path = self.root / "ledger.db"
        provision_signed_receipts_store(self.receipts)
        self.ledger = CheckpointLedger(self.ledger_path)
        self.trust_root, self.signers = make_keys()
        self.evidence = FixtureEvidence()
        self.probe = FixtureProbe()
        self.gate = OfflineAdmissionGate(self.admission, self.trust_root, self.evidence, self.probe)

    def adapter(self, halt=("clear",)):
        return OfflineAuthenticatedAdapter.open_signed(
            self.ledger, self.receipts, admission_gate=self.gate, halt_check=HaltCheck(halt),
            trusted_fixture=True,
        )

    def envelope(self, kind="assignment", tag="a", **kwargs):
        return make_envelope(self.signers, kind, tag, **kwargs)

    def frame(self, kind="assignment", tag="a", **kwargs):
        return to_frame(self.envelope(kind, tag, **kwargs))


def _ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)


def durable(stores) -> dict:
    """Read-only snapshot of every durable record an admission attempt can write."""
    state = {}
    con = _ro(stores.receipts)
    try:
        for table, order in (("fail_stop", "singleton"), ("intent_journal", "intent_id"),
                             ("audit_receipts", "receipt_id"), ("sequence_state", "stream_key"),
                             ("assignment_identity", "project_id, task_id")):
            state[table] = con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
    finally:
        con.close()
    if stores.admission.exists():
        con = _ro(stores.admission)
        try:
            state["reservations"] = con.execute("SELECT * FROM reservations ORDER BY id").fetchall()
        finally:
            con.close()
    con = _ro(stores.ledger_path)
    try:
        state["ledger_events"] = con.execute(
            "SELECT revision, event_type, message_id FROM events ORDER BY revision").fetchall()
    finally:
        con.close()
    return state


def released(state):
    return state["fail_stop"] == [(1, 0, None)]


def receipts(state):
    return [(r[1], r[8], r[9]) for r in state["audit_receipts"]]


def intents(state):
    return [(r[1], r[9]) for r in state["intent_journal"]]


# --- labelled seams --------------------------------------------------------------


class RecordingSink:
    """RECORDING SINK seam. 'Recorded' means appended to ``records`` only."""

    def __init__(self, mode="normal", observe=None):
        self.mode = mode
        self.observe = observe
        self.calls = 0
        self.records = []
        self.observed = []

    def record(self, record):
        self.calls += 1
        if self.observe is not None:
            self.observed.append(self.observe())
        if self.mode == "raise_before":
            raise InjectedFault("sink raised before recording")
        if self.mode == "interrupt":
            raise _Interrupt("sink interrupted")
        self.records.append(record)
        if self.mode == "record_then_raise":
            raise InjectedFault("sink recorded, then raised")


def count_calls(adapter):
    """CALL COUNTER seam: counts and delegates every accept_signed invocation."""
    real = adapter.accept_signed
    calls = []

    def wrapped(envelope, *, now=None):
        calls.append((envelope, now))
        return real(envelope, now=now)

    adapter.accept_signed = wrapped
    return calls


def spy_gate(stores, mode=None):
    """WHOLE-CALL gate.reserve wrapper; the internal gate commit seam is not faulted."""
    real = stores.gate.reserve
    counts = {"reserve": 0}

    def wrapped(snap, now):
        counts["reserve"] += 1
        if mode == "raise_without_delegating":
            raise InjectedFault("gate.reserve not invoked")
        return real(snap, now)

    stores.gate.reserve = wrapped
    return counts


def spy_ledger(stores, mode=None):
    """WHOLE-CALL ledger.record_event wrapper; the internal ledger commit seam is not faulted."""
    real = stores.ledger.record_event
    counts = {"record_event": 0}

    def wrapped(**kwargs):
        counts["record_event"] += 1
        if mode == "raise_without_delegating":
            raise InjectedFault("record_event not invoked")
        result = real(**kwargs)
        if mode == "delegate_then_raise":
            raise InjectedFault("record_event returned, then wrapper raised")
        return result

    stores.ledger.record_event = wrapped
    return counts


class _Plan:
    def __init__(self, fault=None):
        self.fault = fault          # (label, 'a' | 'c') or None
        self.fired = False
        self.armed = []             # G values of every arm statement executed


class _ScriptedConnection:
    """Receipts connection wrapper on the private P-6 factory seam."""

    TARGETS = {"W3": "UPDATE intent_journal SET state = 'ACCEPTED'", "R": "UPDATE fail_stop SET stopped = 0"}

    def __init__(self, real, plan):
        self._real = real
        self._plan = plan
        self._statements = []

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def _label(self):
        joined = " | ".join(self._statements)
        if "UPDATE fail_stop SET stopped = 0" in joined:
            return "R"
        if "SET state = 'ACCEPTED'" in joined:
            return "W3"
        if "UPDATE fail_stop SET stopped = 1" in joined:
            return "A"
        if "INSERT INTO intent_journal" in joined:
            return "W1"
        return None

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        if flat.startswith("BEGIN"):
            self._statements = []
        if flat.startswith("UPDATE fail_stop SET stopped = 1"):
            self._plan.armed.append(params[0])
        cursor = self._real.execute(sql, params)
        if not flat.startswith(("BEGIN", "SELECT", "PRAGMA")):
            self._statements.append(flat)
        fault = self._plan.fault
        if (fault is not None and not self._plan.fired and fault[1] == "a"
                and flat.startswith(self.TARGETS[fault[0]])):
            self._plan.fired = True
            raise InjectedFault(f"injected pre-commit failure at {fault[0]}")
        return cursor

    def commit(self):
        fault = self._plan.fault
        if fault is not None and not self._plan.fired and fault[1] == "c" and self._label() == fault[0]:
            self._plan.fired = True
            self._real.commit()
            raise InjectedFault(f"injected failure after real commit at {fault[0]}")
        self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()


def install_plan(adapter, plan):
    real_factory = adapter._receipts_connect
    adapter._receipts_connect = lambda: _ScriptedConnection(real_factory(), plan)
    return plan


class DoubleAdapter:
    """UNKNOWN-ADAPTER DOUBLE seam: outcomes outside the real adapter's behavior."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def accept_signed(self, envelope, *, now=None):
        self.calls.append((envelope, now))
        return self.behavior()


def _raise(exc):
    def behavior():
        raise exc
    return behavior


def _return(value):
    return lambda: value


def admit(stores, adapter, kind="assignment", tag="a", **kwargs):
    result = deliver(stores.frame(kind, tag, **kwargs), adapter, RecordingSink(), NOW)
    assert result.admission is Admission.ADMITTED, result
    return result


def assert_not_attempted(result):
    assert result == IngressResult(
        admission=Admission.MALFORMED, adapter_step=None, adapter_classification=None, poisoned=None,
        adapter_calls=0, correlation=None, correlation_verified=False, ack=Ack.NOT_ATTEMPTED,
        detail=result.detail,
    )
    assert result.detail.startswith("malformed: ")


# =============================================================================
# Case 1 - decoder round-trip controls (real signed envelopes)
# =============================================================================

ESCAPES = {"note": 'q"uote \\ back/slash \n\t\r \u00e9 \u4e2d \u2028 \U0001f600 [[{{ "}} \\"[',
           "\u00e9t\u00e9": "key escapes", "empty": "", "neg": -5, "list": [1, "two", [3], {"x": "y"}]}


@pytest.mark.parametrize("kind", sorted(KINDS))
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_case1_round_trip_real_signed_envelopes_fields_and_canonical_bytes(tmp_path, kind, ensure_ascii):
    trust_root, signers = make_keys()
    env = make_envelope(signers, kind, payload_set=ESCAPES)
    frame = to_frame(env, ensure_ascii=ensure_ascii)
    decoded = decode_signed_frame(frame)
    assert decoded == env
    for field in dataclasses.fields(SignedEnvelope):
        assert type(getattr(decoded, field.name)) is type(getattr(env, field.name))
    assert decoded.timestamp.tzinfo is timezone.utc and decoded.expiry.tzinfo is timezone.utc
    assert canonical_bytes(decoded) == canonical_bytes(env)
    assert verify_signature(decoded, trust_root, NOW) == "cryptographically_valid"


def test_case1_exact_frame_byte_limit_65536_accepted_65537_malformed(tmp_path):
    trust_root, signers = make_keys()
    env = make_envelope(signers, payload_set={"pad": "x" * 60000})
    base = to_frame(env)
    exact = base[:-1] + b" " * (ingress.MAX_FRAME_BYTES - len(base)) + b"}"
    assert len(exact) == 65536
    decoded = decode_signed_frame(exact)
    assert decoded == env and canonical_bytes(decoded) == canonical_bytes(env)
    assert verify_signature(decoded, trust_root, NOW) == "cryptographically_valid"
    over = base[:-1] + b" " * (ingress.MAX_FRAME_BYTES + 1 - len(base)) + b"}"
    assert len(over) == 65537
    with pytest.raises(MalformedFrame, match="exceeds 65536 bytes"):
        decode_signed_frame(over)


def _nested(levels):
    value = 1
    for _ in range(levels):
        value = [value]
    return value


def test_case1_depth_32_accepted_33_malformed_and_brackets_in_strings_not_counted(tmp_path):
    trust_root, signers = make_keys()
    # Top-level object = 1, payload object = 2, then 30 lists = depth 32.
    env = make_envelope(signers, payload_set={"n": _nested(30), "s": '\\"[[[[{{{{' * 40})
    decoded = decode_signed_frame(to_frame(env))
    assert decoded == env and canonical_bytes(decoded) == canonical_bytes(env)
    assert verify_signature(decoded, trust_root, NOW) == "cryptographically_valid"
    deep = make_envelope(signers, payload_set={"n": _nested(31)})
    with pytest.raises(MalformedFrame, match="nesting depth exceeds 32"):
        decode_signed_frame(to_frame(deep))


# =============================================================================
# Case 2 - valid frame through the real signed adapter
# =============================================================================


def test_case2_valid_frames_admitted_once_with_one_confirmed_sink_record(tmp_path):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    calls = count_calls(adapter)
    for kind in ("assignment", "implementation", "review"):
        before = durable(stores)
        sink = RecordingSink(observe=lambda: durable(stores))
        env = stores.envelope(kind)
        result = deliver(to_frame(env), adapter, sink, NOW)
        correlation = Correlation(env.message_id, env.idempotency_key, env.sequence)
        assert result == IngressResult(
            admission=Admission.ADMITTED, adapter_step=None, adapter_classification="accepted",
            poisoned=None, adapter_calls=1, correlation=correlation, correlation_verified=True,
            ack=Ack.CONFIRMED_BY_SINK, detail="accepted",
        )
        assert sink.records == [AckRecord(1, Admission.ADMITTED, None, correlation, True)]
        after = durable(stores)
        assert sink.calls == 1 and sink.observed == [after]
        assert released(after)
        assert len(after["ledger_events"]) == len(before["ledger_events"]) + 1
        assert after["ledger_events"][-1][2] == env.message_id
        assert receipts(after)[-1] == (env.message_id, "accepted", None)
        assert intents(after)[-1] == (env.message_id, "ACCEPTED")
    assert len(calls) == 3
    assert [c[1] for c in calls] == [NOW] * 3


# =============================================================================
# Case 3 - identical and conflicting replay
# =============================================================================


def test_case3_identical_and_conflicting_replay_rejected_by_existing_s6(tmp_path):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    admit(stores, adapter)
    calls = count_calls(adapter)
    events = durable(stores)["ledger_events"]
    variants = [
        ("identical", stores.frame(), REPLAY),
        ("same_message_id_other_content", stores.frame(payload_set={"objective": "other"}), REPLAY),
        ("same_idempotency_key_new_message", stores.frame(mid="ing-assignment-new"), REPLAY),
        ("reused_sequence_new_identity", stores.frame("assignment", "fresh", sequence=1), "sequence 1 != 2"),
    ]
    for name, frame, reason in variants:
        sink = RecordingSink()
        result = deliver(frame, adapter, sink, NOW)
        assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == (
            Admission.REJECTED, "E1.b", "rejected", False), name
        assert result.correlation_verified is True and result.ack is Ack.CONFIRMED_BY_SINK
        state = durable(stores)
        assert receipts(state)[-1][1:] == ("rejected", reason), name
        assert state["ledger_events"] == events and released(state)
    assert len(calls) == len(variants)


# =============================================================================
# Case 4 - stage split (S4 / assignment S6 / non-assignment gate after A)
# =============================================================================

S4_CASES = {
    "bad_signature": dict(bad_signature=True),
    "unknown_key": dict(key=("test-b2b2-ingress-unknown", SigningKey(b"\x7f" * 32))),
    "actor_identity_binding": dict(sign_as="jason"),
}


@pytest.mark.parametrize("case", sorted(S4_CASES))
def test_case4a_s4_refusal_zero_receipts_zero_arm(tmp_path, case):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    plan = install_plan(adapter, _Plan())
    gate = spy_gate(stores)
    before = durable(stores)
    result = deliver(stores.frame(**S4_CASES[case]), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == (
        Admission.REFUSED, "T1", "refused", False)
    assert result.correlation_verified is False and result.ack is Ack.CONFIRMED_BY_SINK
    assert durable(stores) == before and plan.armed == [] and gate == {"reserve": 0}


S6_CASES = {
    "assignment_tuple": (dict(recipient="skippy"), None, "Assignment authorization tuple not permitted"),
    "assignment_evidence": ({}, "rejected", "Assignment evidence rejected"),
}


@pytest.mark.parametrize("case", sorted(S6_CASES))
def test_case4b_assignment_s6_verified_denial_never_arms(tmp_path, case):
    stores = Stores(tmp_path)
    kwargs, verdict, reason = S6_CASES[case]
    if verdict is not None:
        stores.evidence.verdict = verdict
    adapter = stores.adapter()
    plan = install_plan(adapter, _Plan())
    gate = spy_gate(stores)
    env = stores.envelope(**kwargs)
    result = deliver(to_frame(env), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == (
        Admission.REJECTED, "E1.b", "rejected", False)
    assert result.correlation_verified is True
    state = durable(stores)
    assert receipts(state) == [(env.message_id, "rejected", reason)]
    assert plan.armed == [] and gate == {"reserve": 0} and released(state)
    assert state["intent_journal"] == [] and state["ledger_events"] == [] and state["reservations"] == []


S10_CASES = {
    "unauthorized_implementation_tuple": (dict(recipient="chief"), False,
                                          "admission: unauthorized sender/role/recipient/type combination"),
    "rejected_non_assignment_evidence": ({}, True, "admission: evidence rejected"),
}


@pytest.mark.parametrize("case", sorted(S10_CASES))
def test_case4c_non_assignment_denial_arms_then_gate_s10_and_release(tmp_path, case):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    plan = install_plan(adapter, _Plan())
    admit(stores, adapter)
    kwargs, reject_evidence, reason = S10_CASES[case]
    if reject_evidence:
        stores.evidence.verdict = "rejected"
    gate = spy_gate(stores)
    before = durable(stores)
    env = stores.envelope("implementation", **kwargs)
    result = deliver(to_frame(env), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == (
        Admission.REJECTED, "S10", "rejected", False)
    assert result.correlation_verified is True
    state = durable(stores)
    assert len(plan.armed) == 2 and gate == {"reserve": 1} and released(state)
    assert receipts(state)[-1] == (env.message_id, "rejected", reason)
    # The existing assignment event is not a new event of this rejection.
    assert state["ledger_events"] == before["ledger_events"] and len(before["ledger_events"]) == 1
    assert state["intent_journal"] == before["intent_journal"]


def test_case4c_injected_gate_uncertainty_retains_g_and_poisons(tmp_path):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    plan = install_plan(adapter, _Plan())
    admit(stores, adapter)
    gate = spy_gate(stores, "raise_without_delegating")
    calls = count_calls(adapter)
    before = durable(stores)
    frame = stores.frame("implementation", recipient="chief")
    result = deliver(frame, adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == (
        Admission.UNCERTAIN, "S10", "ambiguous", True)
    assert result.correlation_verified is False
    state = durable(stores)
    assert state["fail_stop"] == [(1, 1, plan.armed[-1])] and gate == {"reserve": 1}
    assert state["ledger_events"] == before["ledger_events"]
    again = deliver(frame, adapter, RecordingSink(), NOW)
    assert (again.admission, again.adapter_step, again.poisoned) == (Admission.REFUSED, "S2", True)
    assert durable(stores) == state and len(calls) == 2 and gate == {"reserve": 1}


# =============================================================================
# Case 5 - HALT outcomes stay distinct
# =============================================================================


def test_case5_s5_halt_is_pre_arm_e1b_denial(tmp_path):
    stores = Stores(tmp_path)
    adapter = stores.adapter(halt=("halted",))
    plan = install_plan(adapter, _Plan())
    env = stores.envelope()
    result = deliver(to_frame(env), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification) == (
        Admission.REJECTED, "E1.b", "rejected")
    state = durable(stores)
    assert receipts(state) == [(env.message_id, "rejected", "HALTED")]
    assert plan.armed == [] and released(state) and state["ledger_events"] == []


def test_case5_s5_halt_check_failure_writes_uncertain_receipt_classified_rejected(tmp_path):
    stores = Stores(tmp_path)
    adapter = stores.adapter(halt=("raise",))
    plan = install_plan(adapter, _Plan())
    env = stores.envelope()
    result = deliver(to_frame(env), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification) == (
        Admission.REJECTED, "E1.b", "rejected")
    state = durable(stores)
    assert receipts(state) == [(env.message_id, "uncertain", "HALT check failed: RuntimeError")]
    assert plan.armed == [] and released(state)


def test_case5_s9_halt_at_reacquisition_is_stop_retained(tmp_path):
    stores = Stores(tmp_path)
    adapter = stores.adapter(halt=("clear", "halted"))
    plan = install_plan(adapter, _Plan())
    env = stores.envelope()
    result = deliver(to_frame(env), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == (
        Admission.STOP_RETAINED, "S9", "stop_retained", True)
    assert result.correlation_verified is True
    state = durable(stores)
    assert state["fail_stop"] == [(1, 1, plan.armed[0])]
    assert receipts(state) == [(env.message_id, "rejected", "HALTED at reacquisition (S9); guard retained")]
    assert state["ledger_events"] == [] and state["reservations"] == [] and state["intent_journal"] == []


def test_case5_gate_halt_probe_rejects_at_s10_and_releases(tmp_path):
    stores = Stores(tmp_path)
    stores.probe.state = "halted"
    adapter = stores.adapter()
    plan = install_plan(adapter, _Plan())
    env = stores.envelope()
    result = deliver(to_frame(env), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification) == (
        Admission.REJECTED, "S10", "rejected")
    state = durable(stores)
    assert receipts(state) == [(env.message_id, "rejected", "admission: HALT state halted")]
    assert len(plan.armed) == 1 and released(state) and state["ledger_events"] == []


# =============================================================================
# Case 6 - durable uncertainty through existing seams
# =============================================================================

UNCERTAIN_CASES = {
    # name: (ledger mode, gate mode, receipts fault, step, classification, new ledger events, intent)
    "gate_whole_call_before_ledger": (None, "raise_without_delegating", None, "S10", "ambiguous", 0, None),
    "w2_raise_before_ledger": ("raise_without_delegating", None, None, "W2", "ambiguous", 0, "PENDING"),
    "w2_after_real_ledger_write": ("delegate_then_raise", None, None, "W2", "ambiguous", 1, "PENDING"),
    "w3_after_real_commit": (None, None, ("W3", "c"), "W3", "ambiguous", 1, "ACCEPTED"),
    "r_confirmed_rolled_back_after_ledger": (None, None, ("R", "a"), "R", "confirmed_rolled_back", 1, "ACCEPTED"),
}


def _uncertain_setup(stores, adapter, case):
    ledger_mode, gate_mode, fault, *_ = UNCERTAIN_CASES[case]
    plan = install_plan(adapter, _Plan(fault))
    if ledger_mode:
        spy_ledger(stores, ledger_mode)
    if gate_mode:
        spy_gate(stores, gate_mode)
    return plan


@pytest.mark.parametrize("case", sorted(UNCERTAIN_CASES))
def test_case6_durable_uncertainty_keeps_raw_classification_poisons_and_never_retries(tmp_path, case):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    plan = _uncertain_setup(stores, adapter, case)
    *_, step, classification, new_events, intent = UNCERTAIN_CASES[case]
    calls = count_calls(adapter)
    env = stores.envelope()
    result = deliver(to_frame(env), adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == (
        Admission.UNCERTAIN, step, classification, True)
    assert result.correlation_verified is False and result.adapter_calls == 1
    state = durable(stores)
    assert len(state["ledger_events"]) == new_events
    assert intents(state) == ([] if intent is None else [(env.message_id, intent)])
    assert state["fail_stop"] == [(1, 1, plan.armed[0])]
    again = deliver(stores.frame("assignment", "next"), adapter, RecordingSink(), NOW)
    assert (again.admission, again.adapter_step, again.poisoned) == (Admission.REFUSED, "S2", True)
    assert durable(stores) == state and len(calls) == 2


# =============================================================================
# Case 7 - sink matrix: admission and acknowledgment independent
# =============================================================================

SINK_MODES = {"raise_before": Ack.NOT_CONFIRMED, "record_then_raise": Ack.NOT_CONFIRMED,
              "normal": Ack.CONFIRMED_BY_SINK}
# outcome: (admission, step, classification, new ledger events, G released, redelivery result)
SINK_OUTCOMES = {
    "admitted": (Admission.ADMITTED, None, "accepted", 1, True, (Admission.REJECTED, "E1.b")),
    "refused_on_existing_stream": (Admission.REFUSED, "T1", "refused", 0, True, (Admission.REFUSED, "T1")),
    "rejected_replay": (Admission.REJECTED, "E1.b", "rejected", 0, True, (Admission.REJECTED, "E1.b")),
    "uncertain_before_ledger": (Admission.UNCERTAIN, "W2", "ambiguous", 0, False, (Admission.REFUSED, "S2")),
    "uncertain_after_real_ledger_write": (Admission.UNCERTAIN, "W2", "ambiguous", 1, False,
                                          (Admission.REFUSED, "S2")),
}


def _sink_subject(stores, adapter, outcome):
    if outcome == "admitted":
        return stores.frame()
    if outcome == "refused_on_existing_stream":
        admit(stores, adapter)
        return stores.frame("implementation", bad_signature=True)
    if outcome == "rejected_replay":
        admit(stores, adapter)
        return stores.frame()
    spy_ledger(stores, "raise_without_delegating" if outcome == "uncertain_before_ledger"
               else "delegate_then_raise")
    return stores.frame()


@pytest.mark.parametrize("mode", sorted(SINK_MODES))
@pytest.mark.parametrize("outcome", sorted(SINK_OUTCOMES))
def test_case7_sink_matrix_preserves_admission_and_durable_state(tmp_path, outcome, mode):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    frame = _sink_subject(stores, adapter, outcome)
    admission, step, classification, new_events, is_released, redelivery = SINK_OUTCOMES[outcome]
    calls = count_calls(adapter)
    before = durable(stores)
    sink = RecordingSink(mode, observe=lambda: durable(stores))
    result = deliver(frame, adapter, sink, NOW)
    after = durable(stores)
    assert (result.admission, result.adapter_step, result.adapter_classification) == (
        admission, step, classification)
    assert result.ack is SINK_MODES[mode] and result.adapter_calls == 1
    assert len(calls) == 1 and sink.calls == 1
    # The outcome's own baseline-relative durable state is fixed before the sink
    # runs, and the sink neither changes it nor triggers any further call.
    assert sink.observed == [after]
    assert len(after["ledger_events"]) - len(before["ledger_events"]) == new_events
    assert released(after) is is_released
    expected = AckRecord(1, admission, step, result.correlation, result.correlation_verified)
    assert sink.records == ([] if mode == "raise_before" else [expected])
    if mode != "normal":
        assert "sink raised InjectedFault" in result.detail
    # Redelivery is a new deliver call under existing state/instance rules.
    again = deliver(frame, adapter, RecordingSink(), NOW)
    assert (again.admission, again.adapter_step) == redelivery
    assert durable(stores)["ledger_events"] == after["ledger_events"]
    assert len(calls) == 2 and sink.calls == 1


def test_case7_interrupts_are_not_swallowed(tmp_path):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    calls = count_calls(adapter)
    sink = RecordingSink("interrupt")
    with pytest.raises(_Interrupt):
        deliver(stores.frame(), adapter, sink, NOW)
    assert len(calls) == 1 and sink.calls == 1 and sink.records == []
    double = DoubleAdapter(_raise(_Interrupt("adapter interrupted")))
    sink = RecordingSink()
    with pytest.raises(_Interrupt):
        deliver(stores.frame("assignment", "b"), double, sink, NOW)
    assert len(double.calls) == 1 and sink.calls == 0


# =============================================================================
# Case 8 - malformed frames and unexpected adapter outcomes
# =============================================================================


def _base(**payload_set):
    _, signers = make_keys()
    return frame_obj(make_envelope(signers, payload_set=payload_set or None))


def _text(obj=None):
    return dump(obj if obj is not None else _base()).decode("utf-8")


def _with(field, value):
    obj = _base()
    obj[field] = value
    return dump(obj)


def _without(field):
    obj = _base()
    del obj[field]
    return dump(obj)


def _sub(old, new, obj=None):
    text = _text(obj)
    assert old in text
    return text.replace(old, new, 1).encode("utf-8")


def _bsub(old, new):
    raw = dump(_base())
    assert old in raw
    return raw.replace(old, new, 1)


def _pad(total):
    raw = dump(_base())
    return raw[:-1] + b" " * (total - len(raw)) + b"}"


MALFORMED = {
    "not_bytes_str": lambda: _text(),
    "not_bytes_bytearray": lambda: bytearray(dump(_base())),
    "not_bytes_memoryview": lambda: memoryview(dump(_base())),
    "not_bytes_none": lambda: None,
    "oversize_65537": lambda: _pad(65537),
    "empty": lambda: b"",
    "whitespace_only": lambda: b"  \r\n ",
    "invalid_utf8": lambda: _bsub(b'"objective"', b'"obj\xffective"'),
    "overlong_utf8": lambda: _bsub(b'"objective"', b'"obj\xc0\xafective"'),
    "encoded_surrogate_utf8": lambda: _bsub(b'"objective"', b'"obj\xed\xa0\x80ective"'),
    "bom": lambda: b"\xef\xbb\xbf" + dump(_base()),
    "duplicate_top_level_key": lambda: _sub('"sender":"chief"', '"sender":"chief","sender":"chief"'),
    "duplicate_payload_key": lambda: _sub('"objective":', '"objective":"x","objective":'),
    "duplicate_deep_key": lambda: _sub('"b":1', '"b":1,"b":1', _base(n={"a": {"b": 1}})),
    "trailing_token": lambda: (_text() + " x").encode("utf-8"),
    "trailing_object": lambda: (_text() + _text()).encode("utf-8"),
    "nan": lambda: _sub('"v":0', '"v":NaN', _base(v=0)),
    "infinity": lambda: _sub('"v":0', '"v":Infinity', _base(v=0)),
    "negative_infinity": lambda: _sub('"v":0', '"v":-Infinity', _base(v=0)),
    "float": lambda: _sub('"v":0', '"v":1.0', _base(v=0)),
    "exponent": lambda: _sub('"v":0', '"v":1e2', _base(v=0)),
    "negative_zero_float": lambda: _sub('"v":0', '"v":-0.0', _base(v=0)),
    "huge_integer": lambda: _sub('"sequence":1', '"sequence":' + "9" * 5000),
    "lone_surrogate_value": lambda: _sub('"sender":"chief"', '"sender":"\\ud800"'),
    "lone_surrogate_payload_key": lambda: _sub('"objective":', '"\\udc00":1,"objective":'),
    "depth_33": lambda: dump(_base(n=_nested(31))),
    "top_level_array": lambda: b"[]",
    "top_level_string": lambda: b'"x"',
    "missing_field": lambda: _without("key_id"),
    "extra_field": lambda: _with("extra", 1),
    "bool_sequence": lambda: _with("sequence", True),
    "bool_schema_version": lambda: _with("schema_version", False),
    "string_sequence": lambda: _with("sequence", "1"),
    "null_header": lambda: _with("key_id", None),
    "int_header": lambda: _with("sender", 1),
    "payload_list": lambda: _with("payload", []),
    "payload_null": lambda: _with("payload", None),
    "digest_uppercase": lambda: _with("payload_digest", _base()["payload_digest"].upper()),
    "digest_63": lambda: _with("payload_digest", _base()["payload_digest"][:-1]),
    "digest_65": lambda: _with("payload_digest", _base()["payload_digest"] + "0"),
    "digest_non_hex": lambda: _with("payload_digest", "g" * 64),
    "signature_127": lambda: _with("signature", _base()["signature"][:-1]),
    "signature_uppercase": lambda: _with("signature", _base()["signature"].upper()),
    "signature_as_list": lambda: _with("signature", [0] * 64),
    "timestamp_offset": lambda: _with("timestamp", "2026-11-14T11:59:00+00:00"),
    "timestamp_fraction": lambda: _with("timestamp", "2026-11-14T11:59:00.5Z"),
    "timestamp_lowercase_z": lambda: _with("timestamp", "2026-11-14T11:59:00z"),
    "timestamp_space": lambda: _with("timestamp", " 2026-11-14T11:59:00Z"),
    "timestamp_invalid_date": lambda: _with("timestamp", "2026-02-30T11:59:00Z"),
    "timestamp_hour_24": lambda: _with("timestamp", "2026-11-14T24:00:00Z"),
    "timestamp_second_60": lambda: _with("timestamp", "2026-11-14T11:59:60Z"),
    "timestamp_year_zero": lambda: _with("timestamp", "0000-01-01T00:00:00Z"),
    "timestamp_non_ascii_digit": lambda: _with("timestamp", "\u0662026-11-14T11:59:00Z"),
    "expiry_number": lambda: _with("expiry", 1763121540),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_case8_malformed_frame_zero_adapter_and_sink_calls(tmp_path, case):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    calls = count_calls(adapter)
    sink = RecordingSink()
    before, files = durable(stores), sorted(os.listdir(tmp_path))
    frame = MALFORMED[case]()
    with pytest.raises(MalformedFrame):
        decode_signed_frame(frame)
    result = deliver(frame, adapter, sink, NOW)
    assert_not_attempted(result)
    assert calls == [] and sink.calls == 0 and sink.records == []
    assert durable(stores) == before and sorted(os.listdir(tmp_path)) == files
    # The same instance still admits a valid frame afterwards (no hidden state).
    assert deliver(stores.frame(), adapter, RecordingSink(), NOW).admission is Admission.ADMITTED


NO_REPAIR = {
    "payload_boolean": ("payload", lambda obj: {**obj["payload"], "flag": True}),
    "payload_null_value": ("payload", lambda obj: {**obj["payload"], "flag": None}),
    "non_nfc_sender": ("sender", lambda obj: "che\u0301f"),
    "padded_header": ("message_id", lambda obj: " " + obj["message_id"] + " "),
    "empty_header": ("key_id", lambda obj: ""),
    "schema_version_2": ("schema_version", lambda obj: 2),
    "negative_sequence": ("sequence", lambda obj: -1),
    "digest_mismatch": ("payload_digest", lambda obj: "0" * 64),
    "expired": ("expiry", lambda obj: "2026-11-14T11:59:30Z"),
}


@pytest.mark.parametrize("case", sorted(NO_REPAIR))
def test_case8_profile_permitted_signed_violations_pass_unchanged_and_s4_refuses(tmp_path, case):
    stores = Stores(tmp_path)
    adapter = stores.adapter()
    calls = count_calls(adapter)
    field, mutate = NO_REPAIR[case]
    obj = frame_obj(stores.envelope())
    obj[field] = mutate(obj)
    frame = dump(obj)
    decoded = decode_signed_frame(frame)
    value = getattr(decoded, field)
    if field == "payload_digest":
        assert value == bytes.fromhex(obj[field])
    elif field == "expiry":
        assert value.strftime("%Y-%m-%dT%H:%M:%SZ") == obj[field]
    else:
        assert value == obj[field] and type(value) is type(obj[field])
    before = durable(stores)
    result = deliver(frame, adapter, RecordingSink(), NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification) == (
        Admission.REFUSED, "T1", "refused")
    assert len(calls) == 1 and calls[0][0] == decoded and durable(stores) == before


UNEXPECTED = {
    "ordinary_exception": (_raise(RuntimeError("boom")), (Admission.UNCERTAIN, None, None, None)),
    "unknown_classification": (_raise(SignedAdmissionError("x", step="T1", classification="weird")),
                               (Admission.UNCERTAIN, "T1", None, False)),
    "unknown_step": (_raise(SignedAdmissionError("x", step="Z9", classification="refused")),
                     (Admission.UNCERTAIN, "Z9", "refused", False)),
    "post_arm_owner_refusal": (_raise(SignedAdmissionError("x", step="T3", classification="refused",
                                                           poisoned=True)),
                               (Admission.UNCERTAIN, "T3", "refused", True)),
    "confirmed_rolled_back": (_raise(SignedAdmissionError("x", step="E1.b",
                                                          classification="confirmed_rolled_back", poisoned=True)),
                              (Admission.UNCERTAIN, "E1.b", "confirmed_rolled_back", True)),
    "rejected_at_unknown_step": (_raise(SignedAdmissionError("x", step="T2", classification="rejected")),
                                 (Admission.UNCERTAIN, "T2", "rejected", False)),
    "non_string_step_and_poison": (_raise(type("Odd", (SignedAdmissionError,), {})(
        "x", step=7, classification="ambiguous", poisoned="yes")), (Admission.UNCERTAIN, None, "ambiguous", None)),
    "pre_arm_refusal_control": (_raise(SignedAdmissionError("x", step="T1", classification="refused")),
                                (Admission.REFUSED, "T1", "refused", False)),
    "return_uppercase": (_return("ACCEPTED"), (Admission.UNCERTAIN, None, None, None)),
    "return_none": (_return(None), (Admission.UNCERTAIN, None, None, None)),
    "return_str_subclass": (_return(type("S", (str,), {})("accepted")), (Admission.UNCERTAIN, None, None, None)),
}


@pytest.mark.parametrize("case", sorted(UNEXPECTED))
def test_case8_unexpected_adapter_outcomes_are_uncertain_not_refusals(case):
    _, signers = make_keys()
    behavior, expected = UNEXPECTED[case]
    double = DoubleAdapter(behavior)
    sink = RecordingSink()
    env = make_envelope(signers)
    result = deliver(to_frame(env), double, sink, NOW)
    assert (result.admission, result.adapter_step, result.adapter_classification, result.poisoned) == expected
    assert result.correlation_verified is False or expected[0] is Admission.ADMITTED
    assert result.correlation == Correlation(env.message_id, env.idempotency_key, env.sequence)
    assert double.calls == [(env, NOW)] and sink.calls == 1 and result.ack is Ack.CONFIRMED_BY_SINK
    assert sink.records == [AckRecord(1, expected[0], expected[1], result.correlation, False)]


# =============================================================================
# Closed sets, immutability and source checks
# =============================================================================


def test_closed_sets_and_frozen_result_and_record():
    assert [m.value for m in Admission] == ["MALFORMED", "ADMITTED", "REFUSED", "REJECTED",
                                            "STOP_RETAINED", "UNCERTAIN"]
    assert [m.value for m in Ack] == ["NOT_ATTEMPTED", "CONFIRMED_BY_SINK", "NOT_CONFIRMED"]
    assert [f.name for f in dataclasses.fields(IngressResult)] == [
        "admission", "adapter_step", "adapter_classification", "poisoned", "adapter_calls",
        "correlation", "correlation_verified", "ack", "detail"]
    assert [f.name for f in dataclasses.fields(AckRecord)] == [
        "record_version", "admission", "adapter_step", "correlation", "correlation_verified"]
    assert [f.name for f in dataclasses.fields(Correlation)] == ["message_id", "idempotency_key", "sequence"]
    assert tuple(f.name for f in dataclasses.fields(SignedEnvelope)) == ingress.ENVELOPE_FIELDS
    record = AckRecord(1, Admission.ADMITTED, None, Correlation("m", "i", 1), True)
    result = deliver(b"{}", DoubleAdapter(_return("accepted")), RecordingSink(), NOW)
    for obj, name in ((record, "admission"), (record.correlation, "sequence"), (result, "ack")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, name, None)


def test_source_check_ingress_has_no_store_credential_network_transport_runner_or_retry_path():
    source = Path(ingress.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
            if node.module == "skippy.collaboration.offline_authenticated_adapter":
                assert [a.name for a in node.names] == ["SignedAdmissionError"]
            if node.module == "skippy.collaboration.offline_signature_verifier":
                assert [a.name for a in node.names] == ["SignedEnvelope"]
    assert imported == {"__future__", "json", "re", "dataclasses", "datetime", "enum",
                        "skippy.collaboration.offline_authenticated_adapter",
                        "skippy.collaboration.offline_signature_verifier"}
    forbidden = {"open_signed", "provision_signed_receipts_store", "CheckpointLedger", "OfflineAdmissionGate",
                 "load_trust_root", "KeyEntry", "SigningKey", "connect", "reserve", "record_event",
                 "_receipts_connect", "open", "urlopen", "socket", "getenv", "environ", "subprocess"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (names | attrs) & forbidden
    calls = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert calls.count("accept_signed") == 1 and calls.count("record") == 1
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert not [f.name for f in functions
                if any(word in f.name.lower() for word in ("retry", "resend", "recover", "lookup", "reack"))]
    deliver_def = next(f for f in functions if f.name == "deliver")
    assert not [n for n in ast.walk(deliver_def) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))]
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree).lower()
    for needle in ("github", "http", "token", "runner", "sqlite", "credential", "password"):
        assert needle not in code
    assert ingress.execution_enabled is False
    assert OfflineAuthenticatedAdapter.execution_enabled is False
    assert OfflineAuthenticatedAdapter.TRANSPORT_ENABLED is False
