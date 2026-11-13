"""BRIDGE-02B.2.2 offline-only integration with the existing B2.1 reservation gate.

No BRIDGE-02A ledger, network transport, production keys, or trading involved.
"""

import ast
import hashlib
import json
import multiprocessing
import os
import sqlite3
import threading
import time
import traceback
import urllib.request
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from skippy.collaboration.authenticated_envelope import AuthenticatedEnvelope
from skippy.collaboration.checkpoint_ledger import CheckpointLedger
from skippy.collaboration.github_http_transport import ProductionHTTPTransport
from skippy.collaboration.offline_admission_gate import AdmissionError, OfflineAdmissionGate
from skippy.collaboration.offline_signature_verifier import (
    KeyEntry, SignedEnvelope, _canonical_json, canonical_bytes, load_trust_root,
)
from skippy.collaboration.offline_bridge02b2_contract import SyntheticContractError, _fingerprint
from skippy.collaboration.offline_bridge02b2_simulator import SyntheticBridgeSimulator
import skippy.collaboration.offline_bridge02b2_simulator as simulator_module
from test_offline_bridge02b2_contract import (
    NOW, BASE, IMPLEMENTATION, CORRECTION, KINDS, NEXT_SEQUENCE, SHA_FIELD,
    WRONG_RECIPIENT, WRONG_SENDER, legacy_envelope, msg, prerequisite_messages,
)

import polymarket.orders as polymarket_orders


class FixturePolicy:
    def __init__(self, result="synthetic_verified"):
        self.result = result

    def check(self, message, now):
        if message.project_id != "QLA" or message.task_id != "task-test":
            return "rejected"
        return self.result


class FixtureEvidence:
    def __init__(self, verdict="verified"):
        self.verdict = verdict

    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        if kind not in {"chief_authorization", "skippy_review"}:
            return "rejected"
        if assignment_id != "assign-test" or bound_sha not in {BASE, IMPLEMENTATION, CORRECTION}:
            return "rejected"
        if not evidence_id.startswith("fixture-"):
            return "rejected"
        return self.verdict


class FixtureHalt:
    def __init__(self, state="clear"):
        self.state = state

    def read(self):
        if self.state == "exception":
            raise RuntimeError("synthetic HALT unavailable")
        return self.state


def make_test_keys(revoked_actor=None):
    actors = {"chief": ("chief", b"\x31" * 32),
              "jason": ("implementer", b"\x32" * 32),
              "skippy": ("reviewer", b"\x33" * 32)}
    signers, entries = {}, []
    for actor, (role, seed) in actors.items():
        key_id = "test-b2b2-" + actor
        signer = SigningKey(seed)
        signers[actor] = (key_id, signer)
        entries.append(KeyEntry(key_id, actor, role, signer.verify_key.encode(),
                                "revoked" if actor == revoked_actor else "active",
                                NOW + timedelta(days=30)))
    return load_trust_root(entries, version=1), signers


def simulator(tmp_path, *, policy=None, evidence=None, halt=None, revoked_actor=None):
    root, signers = make_test_keys(revoked_actor)
    return SyntheticBridgeSimulator(
        database_path=tmp_path / "synthetic-reservations.db",
        trust_root=root, signers=signers,
        evidence_verifier=evidence or FixtureEvidence(),
        halt_probe=halt or FixtureHalt(),
        fixture_policy=policy or FixturePolicy(),
    )


def install_protected_entry_sentinels(monkeypatch):
    """Wrap existing protected entrypoints. Names are not invented."""
    counts = {"ledger.record_event": 0, "transport.__call__": 0,
              "urllib.request.urlopen": 0, "place_limit_order": 0}

    def _sentinel(name):
        def wrapped(*args, **kwargs):
            counts[name] += 1
            raise AssertionError(f"protected entrypoint invoked: {name}")
        return wrapped

    monkeypatch.setattr(CheckpointLedger, "record_event", _sentinel("ledger.record_event"))
    monkeypatch.setattr(ProductionHTTPTransport, "__call__", _sentinel("transport.__call__"))
    monkeypatch.setattr(urllib.request, "urlopen", _sentinel("urllib.request.urlopen"))
    monkeypatch.setattr(polymarket_orders, "place_limit_order", _sentinel("place_limit_order"))
    return counts


@pytest.fixture(autouse=True)
def protected_entry_sentinels(monkeypatch):
    """Named zero-invocation runtime sentinels for EVERY simulator-module test."""
    counts = install_protected_entry_sentinels(monkeypatch)
    yield counts
    assert all(value == 0 for value in counts.values()), counts


def test_complete_workflow_reserves_only(tmp_path, protected_entry_sentinels):
    sim = simulator(tmp_path)
    for kind, seq in [("assignment", 1), ("implementation", 2), ("review", 3)]:
        assert sim.simulate(msg(kind, seq), now=NOW).outcome == "reserved"
    assert sim.snapshot.stage == "CLOSED"
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        assert con.execute("SELECT COUNT(*) FROM reservations WHERE state='RESERVED'").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='intent_journal'").fetchone()[0] == 0
    assert set(protected_entry_sentinels) == {"ledger.record_event", "transport.__call__",
                                              "urllib.request.urlopen", "place_limit_order"}
    assert all(value == 0 for value in protected_entry_sentinels.values()), protected_entry_sentinels


def test_review_correction_workflow(tmp_path, protected_entry_sentinels):
    sim = simulator(tmp_path)
    for kind, seq in [("assignment", 1), ("implementation", 2)]:
        assert sim.simulate(msg(kind, seq), now=NOW).outcome == "reserved"
    review = msg("review", 3)
    review.payload["verdict"] = "changes_requested"
    assert sim.simulate(review, now=NOW).outcome == "reserved"
    assert sim.simulate(msg("correction", 4), now=NOW).outcome == "reserved"
    approved = msg("review", 5)
    approved.payload["implementation_sha"] = CORRECTION
    assert sim.simulate(approved, now=NOW).outcome == "reserved"
    assert sim.snapshot.stage == "CLOSED"
    assert set(protected_entry_sentinels) == {"ledger.record_event", "transport.__call__",
                                              "urllib.request.urlopen", "place_limit_order"}
    assert all(value == 0 for value in protected_entry_sentinels.values()), protected_entry_sentinels


def test_exact_duplicate_is_reverified_and_nonwriting(tmp_path):
    sim = simulator(tmp_path)
    original = msg()
    assert sim.simulate(original, now=NOW).outcome == "reserved"
    snapshot = sim.snapshot
    assert sim.simulate(original, now=NOW).outcome == "duplicate"
    assert sim.snapshot == snapshot
    assert sim.simulate(original, now=NOW + timedelta(hours=2)).outcome == "rejected"
    assert sim.snapshot == snapshot
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        assert con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


def test_invalid_signature_and_revoked_key_fail_closed(tmp_path, monkeypatch):
    sim = simulator(tmp_path)
    original = sim._sign_message
    monkeypatch.setattr(sim, "_sign_message", lambda message: replace(original(message), signature=b"\xff" * 64))
    assert sim.simulate(msg(), now=NOW).outcome == "rejected"
    assert sim.snapshot.stage == "EMPTY"
    (tmp_path / "revoked").mkdir()
    sim2 = simulator(tmp_path / "revoked", revoked_actor="chief")
    assert sim2.simulate(msg(), now=NOW).outcome == "rejected"
    assert sim2.snapshot.stage == "EMPTY"


@pytest.mark.parametrize("evidence_state,halt_state,fixture_state", [
    ("rejected", "clear", "synthetic_verified"),
    ("unavailable", "clear", "synthetic_verified"),
    ("verified", "halted", "synthetic_verified"),
    ("verified", "unavailable", "synthetic_verified"),
    ("verified", "exception", "synthetic_verified"),
    ("verified", "clear", "rejected"),
])
def test_explicit_synthetic_dependencies_fail_closed(tmp_path, evidence_state, halt_state, fixture_state):
    sim = simulator(tmp_path, evidence=FixtureEvidence(evidence_state),
                    halt=FixtureHalt(halt_state), policy=FixturePolicy(fixture_state))
    assert sim.simulate(msg(), now=NOW).outcome == "rejected"
    assert sim.snapshot.stage == "EMPTY"


def test_reject_legacy_and_unauthorized_transition_before_gate(tmp_path, monkeypatch):
    sim = simulator(tmp_path)
    def fail(*a, **kw):
        raise AssertionError("gate must not be invoked")
    monkeypatch.setattr(sim._gate, "reserve", fail)
    assert sim.simulate(object(), now=NOW).outcome == "rejected"
    assert sim.simulate(msg("implementation", 1), now=NOW).outcome == "rejected"
    assert sim.snapshot.stage == "EMPTY"


def test_storage_exception_invalidates_fixture_and_denies_restart(tmp_path, monkeypatch):
    """Exception-before-write coverage only: reserve is replaced so no row is committed."""
    sim = simulator(tmp_path)
    def fail(*a, **kw):
        raise AdmissionError("indeterminate commit")
    monkeypatch.setattr(sim._gate, "reserve", fail)
    with pytest.raises(AdmissionError, match="indeterminate"):
        sim.simulate(msg(), now=NOW)
    assert sim.simulate(msg(), now=NOW).outcome == "rejected"
    sim.invalidate_for_restart()
    assert sim.simulate(msg(), now=NOW).outcome == "rejected"
    with pytest.raises(SyntheticContractError, match="new disposable"):
        simulator(tmp_path)


def test_reserve_then_raise_invalidates_and_refuses_reopen(tmp_path, monkeypatch):
    sim = simulator(tmp_path)
    genuine_reserve = sim._gate.reserve

    def reserve_then_raise(envelope, now):
        result = genuine_reserve(envelope, now)
        assert result.outcome == "reserved"
        with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
            assert con.execute(
                "SELECT COUNT(*) FROM reservations WHERE state='RESERVED'"
            ).fetchone()[0] == 1
        raise AdmissionError("indeterminate after committed reservation")

    monkeypatch.setattr(sim._gate, "reserve", reserve_then_raise)
    snapshot_before = sim.snapshot
    with pytest.raises(AdmissionError, match="indeterminate after committed reservation"):
        sim.simulate(msg(), now=NOW)
    assert sim.snapshot == snapshot_before
    assert sim.snapshot.stage == "EMPTY"
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        assert con.execute(
            "SELECT COUNT(*) FROM reservations WHERE state='RESERVED'"
        ).fetchone()[0] == 1

    def fail(*a, **kw):
        raise AssertionError("gate must not be invoked after invalidation")
    monkeypatch.setattr(sim._gate, "reserve", fail)
    assert sim.simulate(msg(), now=NOW).outcome == "rejected"
    with pytest.raises(SyntheticContractError, match="new disposable"):
        simulator(tmp_path)


def test_unresolved_pending_or_uncertain_denies_gate_reopen(tmp_path):
    sim = simulator(tmp_path)
    root, _ = make_test_keys()
    for state in ("PENDING", "UNCERTAIN"):
        with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
            con.execute("DELETE FROM reservations")
            con.execute("""
                INSERT INTO reservations (project_id,task_id,checkpoint_id,assignment_id,
                  message_id,idempotency_key,sender,recipient,role,payload_digest,
                  sequence,state,recorded_at)
                VALUES ('QLA','task-test','check-test','assign-test',
                 'old','old-id','chief','jason','chief',?,1,?,?)
            """, (b"\x00" * 32, state, NOW.isoformat()))
        with pytest.raises(AdmissionError, match=state):
            OfflineAdmissionGate(tmp_path / "synthetic-reservations.db",
                                 root, FixtureEvidence(), FixtureHalt())


def test_concurrent_same_input_at_most_once(tmp_path):
    """Same-instance threading against one simulator RLock, not cross-process exclusion."""
    sim = simulator(tmp_path)
    barrier = threading.Barrier(2)
    outcomes = []
    exceptions = []
    lock = threading.Lock()

    def attempt():
        try:
            barrier.wait(timeout=10)
            try:
                outcome = sim.simulate(msg(), now=NOW).outcome
            except AdmissionError:
                outcome = "failclosed"
            with lock:
                outcomes.append(outcome)
        except Exception as exc:
            with lock:
                exceptions.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads)
    assert exceptions == []
    assert len(outcomes) == 2
    assert outcomes.count("reserved") == 1
    assert set(outcomes) <= {"reserved", "duplicate", "failclosed"}
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        assert con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


def test_no_protected_adapter_or_transport_imports_in_simulator():
    """Supplementary AST import check; runtime sentinels are in a separate test."""
    source = Path(__file__).resolve().parents[1] / "skippy/collaboration/offline_bridge02b2_simulator.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    forbidden = {"skippy.collaboration.offline_authenticated_adapter",
                 "skippy.collaboration.checkpoint_ledger",
                 "skippy.collaboration.github_http_transport"}
    assert not (modules & forbidden)


def test_runtime_sentinels_zero_invocations_on_positive_and_negative_paths(tmp_path, monkeypatch):
    counts = install_protected_entry_sentinels(monkeypatch)
    sim = simulator(tmp_path)
    assert sim.simulate(msg(), now=NOW).outcome == "reserved"
    assert sim.simulate(msg(), now=NOW).outcome == "duplicate"
    assert sim.simulate(msg("implementation", 1), now=NOW).outcome == "rejected"
    (tmp_path / "neg").mkdir()
    sim_neg = simulator(tmp_path / "neg", evidence=FixtureEvidence("rejected"))
    assert sim_neg.simulate(msg(), now=NOW).outcome == "rejected"
    assert all(value == 0 for value in counts.values()), counts


class MutatingPolicy(FixturePolicy):
    def __init__(self, field, value, kinds=None):
        super().__init__()
        self.field = field
        self.value = value
        self.kinds = kinds

    def check(self, message, now):
        result = super().check(message, now)
        if self.kinds is None or message.payload.get("type") in self.kinds:
            message.payload[self.field] = self.value
        return result


MUTATED_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_mutating_policy_on_assignment_is_rejected(tmp_path, monkeypatch):
    sim = simulator(tmp_path, policy=MutatingPolicy("base_sha", MUTATED_SHA))
    snapshot_before = sim.snapshot
    signed = []
    original_sign = sim._sign_message
    def wrap_sign(message):
        signed.append(dict(message.payload))
        return original_sign(message)
    monkeypatch.setattr(sim, "_sign_message", wrap_sign)
    gate_calls = []
    original_reserve = sim._gate.reserve
    def wrap_reserve(*args, **kwargs):
        gate_calls.append(True)
        return original_reserve(*args, **kwargs)
    monkeypatch.setattr(sim._gate, "reserve", wrap_reserve)
    result = sim.simulate(msg(), now=NOW)
    assert result.outcome == "rejected"
    assert "mutated" in result.reason
    assert sim.snapshot == snapshot_before
    assert signed == []
    assert gate_calls == []
    db = tmp_path / "synthetic-reservations.db"
    if db.exists():
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_mutating_policy_on_implementation_is_rejected(tmp_path, monkeypatch):
    policy = MutatingPolicy("implementation_sha", MUTATED_SHA, kinds={"implementation"})
    sim = simulator(tmp_path, policy=policy)
    assert sim.simulate(msg(), now=NOW).outcome == "reserved"
    snapshot_after_assignment = sim.snapshot
    assert snapshot_after_assignment.stage == "AWAIT_IMPLEMENTATION"
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        reservations_before = con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
    assert reservations_before == 1
    signed = []
    original_sign = sim._sign_message
    def wrap_sign(message):
        signed.append(dict(message.payload))
        return original_sign(message)
    monkeypatch.setattr(sim, "_sign_message", wrap_sign)
    gate_calls = []
    original_reserve = sim._gate.reserve
    def wrap_reserve(*args, **kwargs):
        gate_calls.append(True)
        return original_reserve(*args, **kwargs)
    monkeypatch.setattr(sim._gate, "reserve", wrap_reserve)
    result = sim.simulate(msg("implementation", 2), now=NOW)
    assert result.outcome == "rejected"
    assert "mutated" in result.reason
    assert sim.snapshot == snapshot_after_assignment
    assert signed == []
    assert gate_calls == []
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        assert con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == reservations_before


# --- Work order 6845134418: explicit simulator and direct-gate coverage. ---
# Simulator tests drive SyntheticBridgeSimulator over the authentic B2.1 gate.
# Direct-gate tests drive the authentic OfflineAdmissionGate and B1 verifier with
# test-signed SignedEnvelope values and no simulator. Pure-contract evidence stays
# in test_offline_bridge02b2_contract.py. All runs use disposable tmp_path stores.

DB_NAME = "synthetic-reservations.db"
TAMPERED_SHA = CORRECTION
UNKNOWN_KEY_ID = "test-b2b2-unknown"
UNKNOWN_SEED = b"\x34" * 32


def count_rows(db, where=""):
    with closing(sqlite3.connect(db)) as con:
        return con.execute("SELECT COUNT(*) FROM reservations" + where).fetchone()[0]


def forbid_gate(monkeypatch, sim):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise AssertionError("gate must not be invoked")

    monkeypatch.setattr(sim._gate, "reserve", fail)
    return calls


def reserve_prerequisites(sim, kind):
    for message in prerequisite_messages(kind):
        assert sim.simulate(message, now=NOW).outcome == "reserved"


def trust_root_variant(*, revoked=None, expired=None, removed=None):
    """Authentic load_trust_root() variant over the same synthetic test seeds."""
    actors = {"chief": ("chief", b"\x31" * 32),
              "jason": ("implementer", b"\x32" * 32),
              "skippy": ("reviewer", b"\x33" * 32)}
    entries = []
    for actor, (role, seed) in actors.items():
        if actor == removed:
            continue
        entries.append(KeyEntry(
            "test-b2b2-" + actor, actor, role, SigningKey(seed).verify_key.encode(),
            "revoked" if actor == revoked else "active",
            NOW if actor == expired else NOW + timedelta(days=30),
        ))
    return load_trust_root(entries, version=2)


def direct_gate(tmp_path, *, evidence=None, halt=None):
    root, signers = make_test_keys()
    gate = OfflineAdmissionGate(tmp_path / "direct-gate.db", root,
                                evidence or FixtureEvidence(), halt or FixtureHalt())
    return gate, signers


def sign_direct(message, signers, *, key_id=None, signing_key=None):
    """Test-side SignedEnvelope using the B1 verifier's own canonical encoders."""
    default_key_id, default_signer = signers[message.sender]
    payload_bytes = _canonical_json(message.payload)
    unsigned = SignedEnvelope(
        schema_version=1, sender=message.sender, recipient=message.recipient,
        role=message.role, project_id=message.project_id, task_id=message.task_id,
        checkpoint_id=message.checkpoint_id, assignment_id=message.assignment_id,
        message_id=message.message_id, idempotency_key=message.idempotency_key,
        sequence=message.sequence, timestamp=message.timestamp, expiry=message.expiry,
        payload_digest=hashlib.sha256(payload_bytes).digest(),
        payload=dict(message.payload), key_id=key_id or default_key_id,
        signature=b"\x00" * 64,
    )
    signer = signing_key or default_signer
    return replace(unsigned, signature=signer.sign(canonical_bytes(unsigned)).signature)


def hold_write_lock(db):
    """Separate real SQLite connection holding the write lock on a disposable store."""
    locker = sqlite3.connect(db, timeout=0, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    return locker


def corrupt_store(db, how):
    """Checkpoint the disposable WAL store, then damage the real database file."""
    with closing(sqlite3.connect(db)) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if how == "header":
        with open(db, "r+b") as handle:
            handle.write(b"NOT-A-SQLITE-DB!")
    elif how == "truncated":
        with open(db, "r+b") as handle:
            handle.truncate(37)
    else:
        raise AssertionError(how)


# ---- Simulator: legacy object, absent fields, wrong actor (rejected before gate) ----

@pytest.mark.parametrize("kind,seq,stage_kind", [
    ("assignment", 1, "assignment"),
    ("implementation", 2, "implementation"),
    ("correction_request", 3, "correction"),
])
def test_simulator_rejects_authentic_legacy_envelope_before_gate(tmp_path, monkeypatch, kind, seq, stage_kind):
    sim = simulator(tmp_path)
    reserve_prerequisites(sim, stage_kind)
    snapshot, rows = sim.snapshot, count_rows(tmp_path / DB_NAME)
    calls = forbid_gate(monkeypatch, sim)
    legacy = legacy_envelope(kind, seq)
    assert type(legacy) is AuthenticatedEnvelope
    result = sim.simulate(legacy, now=NOW)
    assert result.outcome == "rejected" and "only exact synthetic input" in result.reason
    assert calls == [] and sim.snapshot == snapshot
    assert count_rows(tmp_path / DB_NAME) == rows


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", ["absent_sha", "absent_evidence", "wrong_sender", "wrong_recipient"])
def test_simulator_rejects_absent_fields_and_wrong_actor_before_gate(tmp_path, monkeypatch, kind, case):
    sim = simulator(tmp_path)
    reserve_prerequisites(sim, kind)
    snapshot, rows = sim.snapshot, count_rows(tmp_path / DB_NAME)
    m = msg(kind, NEXT_SEQUENCE[kind])
    if case == "absent_sha":
        del m.payload[SHA_FIELD[kind]]
    elif case == "absent_evidence":
        del m.payload["evidence_id"]
    elif case == "wrong_sender":
        m = replace(m, sender=WRONG_SENDER[kind])
    else:
        m = replace(m, recipient=WRONG_RECIPIENT[kind])
    calls = forbid_gate(monkeypatch, sim)
    result = sim.simulate(m, now=NOW)
    assert result.outcome == "rejected"
    expected = "unauthorized" if case.startswith("wrong") else "unknown or missing"
    assert expected in result.reason
    assert calls == [] and sim.snapshot == snapshot
    assert count_rows(tmp_path / DB_NAME) == rows


def test_simulator_rejects_absent_verdict_before_gate(tmp_path, monkeypatch):
    sim = simulator(tmp_path)
    reserve_prerequisites(sim, "review")
    snapshot = sim.snapshot
    review = msg("review", 3)
    del review.payload["verdict"]
    calls = forbid_gate(monkeypatch, sim)
    result = sim.simulate(review, now=NOW)
    assert result.outcome == "rejected" and "unknown or missing" in result.reason
    assert calls == [] and sim.snapshot == snapshot
    assert count_rows(tmp_path / DB_NAME) == 2


# ---- Simulator: signed tamper, future timestamp, unknown key (rejected by gate) ----

@pytest.mark.parametrize("case,reason", [
    ("payload_tamper", "payload_digest does not match"),
    ("payload_and_digest_tamper", "Invalid signature"),
    ("future_timestamp", "too far in the future"),
    ("unknown_key", "Unknown key_id"),
])
def test_simulator_gate_rejects_tamper_future_time_and_unknown_key(tmp_path, monkeypatch, case, reason):
    sim = simulator(tmp_path)
    message = msg()
    original_sign = sim._sign_message
    if case == "payload_tamper":
        monkeypatch.setattr(sim, "_sign_message", lambda m: replace(
            original_sign(m), payload={**m.payload, "base_sha": TAMPERED_SHA}))
    elif case == "payload_and_digest_tamper":
        def tamper(m):
            env = original_sign(m)
            payload = {**m.payload, "base_sha": TAMPERED_SHA}
            return replace(env, payload=payload,
                           payload_digest=hashlib.sha256(_canonical_json(payload)).digest())
        monkeypatch.setattr(sim, "_sign_message", tamper)
    elif case == "future_timestamp":
        message = msg(timestamp=NOW + timedelta(minutes=2), expiry=NOW + timedelta(hours=2))
    else:
        monkeypatch.setitem(sim._signers, "chief", (UNKNOWN_KEY_ID, SigningKey(UNKNOWN_SEED)))
    result = sim.simulate(message, now=NOW)
    assert result.outcome == "rejected" and reason in result.reason
    assert sim.snapshot.stage == "EMPTY"
    assert count_rows(tmp_path / DB_NAME) == 0


# ---- Simulator: duplicate re-authentication after dependency changes ----

@pytest.mark.parametrize("change,reason", [
    ("halt_halted", "HALT state halted"),
    ("halt_unavailable", "HALT state unavailable"),
    ("halt_exception", "HALT probe failed closed"),
    ("evidence_rejected", "evidence rejected"),
    ("evidence_unavailable", "evidence unavailable"),
    ("key_revoked", "Key not active"),
    ("key_expired", "Key expired"),
    ("key_removed", "Unknown key_id"),
])
def test_duplicate_reauthenticated_after_dependency_change(tmp_path, monkeypatch, change, reason):
    """Each redelivery re-runs B2.1 HALT/signature/evidence checks after reservation."""
    evidence, halt = FixtureEvidence(), FixtureHalt()
    sim = simulator(tmp_path, evidence=evidence, halt=halt)
    reserve_prerequisites(sim, "review")
    duplicate = msg("implementation", 2)
    assert sim.simulate(duplicate, now=NOW).outcome == "duplicate"
    snapshot = sim.snapshot
    assert snapshot.stage == "AWAIT_REVIEW" and count_rows(tmp_path / DB_NAME) == 2
    original_root = sim._gate.trust_root
    if change.startswith("halt_"):
        halt.state = change.removeprefix("halt_")
    elif change.startswith("evidence_"):
        evidence.verdict = change.removeprefix("evidence_")
    else:
        # Test substitution of the gate's injected trust root with an authentic
        # load_trust_root() variant; no production key-rotation API is implied.
        actor = "jason"
        variant = {"key_revoked": dict(revoked=actor), "key_expired": dict(expired=actor),
                   "key_removed": dict(removed=actor)}[change]
        monkeypatch.setattr(sim._gate, "trust_root", trust_root_variant(**variant))
    result = sim.simulate(duplicate, now=NOW)
    assert result.outcome == "rejected" and reason in result.reason
    assert sim.snapshot == snapshot
    assert count_rows(tmp_path / DB_NAME) == 2
    halt.state, evidence.verdict = "clear", "verified"
    monkeypatch.setattr(sim._gate, "trust_root", original_root)
    assert sim.simulate(duplicate, now=NOW).outcome == "duplicate"
    assert sim.snapshot == snapshot and count_rows(tmp_path / DB_NAME) == 2


# ---- Distinct next-sequence competitors (same process; not cross-process proof) ----

def _competitors():
    first = msg("implementation", 2)
    second = msg("implementation", 2, message_id="msg-2b", idempotency_key="idem-2b")
    second.payload.update(evidence_id="fixture-2b", implementation_sha=CORRECTION)
    return first, second


def _race(callables, timeout=30):
    barrier = threading.Barrier(len(callables))
    outcomes, exceptions = {}, []
    lock = threading.Lock()

    def run(name, call):
        try:
            barrier.wait(timeout=timeout)
            try:
                outcome = call()
            except AdmissionError:
                outcome = "failclosed"
            with lock:
                outcomes[name] = outcome
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            with lock:
                exceptions.append(exc)

    threads = [threading.Thread(target=run, args=item) for item in callables.items()]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    assert all(not t.is_alive() for t in threads), "competitor did not finish within finite wait"
    assert time.monotonic() - started < 2 * timeout
    assert exceptions == []
    return outcomes


def test_simulator_distinct_next_sequence_competitors_one_reserves(tmp_path):
    """Same-instance threads against one simulator RLock; not cross-process exclusion."""
    sim = simulator(tmp_path)
    reserve_prerequisites(sim, "implementation")
    first, second = _competitors()
    outcomes = _race({"first": lambda: sim.simulate(first, now=NOW).outcome,
                      "second": lambda: sim.simulate(second, now=NOW).outcome})
    assert sorted(outcomes.values()) == ["rejected", "reserved"]
    winner = first if outcomes["first"] == "reserved" else second
    assert sim.snapshot.stage == "AWAIT_REVIEW" and sim.snapshot.next_sequence == 3
    assert sim.snapshot.latest_sha == winner.payload["implementation_sha"]
    db = tmp_path / DB_NAME
    assert count_rows(db) == 2
    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT message_id FROM reservations WHERE sequence=2").fetchall() == [
            (winner.message_id,)]


def test_direct_gate_distinct_next_sequence_competitors_separate_instances(tmp_path):
    """Two gate instances and SQLite connections in ONE process; not cross-process proof."""
    gate_a, signers = direct_gate(tmp_path)
    assert gate_a.reserve(sign_direct(msg(), signers), NOW).outcome == "reserved"
    root, _ = make_test_keys()
    gate_b = OfflineAdmissionGate(tmp_path / "direct-gate.db", root, FixtureEvidence(), FixtureHalt())
    first, second = (sign_direct(m, signers) for m in _competitors())
    outcomes = _race({"first": lambda: gate_a.reserve(first, NOW).outcome,
                      "second": lambda: gate_b.reserve(second, NOW).outcome})
    assert list(outcomes.values()).count("reserved") == 1
    assert set(outcomes.values()) <= {"reserved", "rejected", "failclosed"}
    assert count_rows(tmp_path / "direct-gate.db") == 2
    assert count_rows(tmp_path / "direct-gate.db", " WHERE sequence=2") == 1


# ---- Real disposable SQLite lock/busy and corruption failures (gate unchanged) ----

def test_simulator_real_sqlite_write_lock_fails_closed_and_invalidates(tmp_path):
    sim = simulator(tmp_path)
    reserve_prerequisites(sim, "implementation")
    snapshot = sim.snapshot
    locker = hold_write_lock(tmp_path / DB_NAME)
    started = time.monotonic()
    try:
        with pytest.raises(AdmissionError, match="storage failure"):
            sim.simulate(msg("implementation", 2), now=NOW)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert time.monotonic() - started < 60
    assert sim.snapshot == snapshot
    assert count_rows(tmp_path / DB_NAME) == 1
    result = sim.simulate(msg("implementation", 2), now=NOW)
    assert result.outcome == "rejected" and "invalidated" in result.reason
    assert count_rows(tmp_path / DB_NAME) == 1


def test_direct_gate_real_sqlite_write_lock_fails_construction_closed(tmp_path):
    gate, signers = direct_gate(tmp_path)
    assert gate.reserve(sign_direct(msg(), signers), NOW).outcome == "reserved"
    root, _ = make_test_keys()
    locker = hold_write_lock(tmp_path / "direct-gate.db")
    started = time.monotonic()
    try:
        with pytest.raises(AdmissionError, match="storage failure"):
            OfflineAdmissionGate(tmp_path / "direct-gate.db", root, FixtureEvidence(), FixtureHalt())
        with pytest.raises(AdmissionError, match="storage failure"):
            gate.reserve(sign_direct(msg("implementation", 2), signers), NOW)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert time.monotonic() - started < 120
    assert count_rows(tmp_path / "direct-gate.db") == 1


@pytest.mark.parametrize("how", ["header", "truncated"])
def test_simulator_real_sqlite_corruption_fails_closed_and_invalidates(tmp_path, how):
    sim = simulator(tmp_path)
    reserve_prerequisites(sim, "implementation")
    snapshot = sim.snapshot
    corrupt_store(tmp_path / DB_NAME, how)
    with pytest.raises(AdmissionError, match="storage failure"):
        sim.simulate(msg("implementation", 2), now=NOW)
    assert sim.snapshot == snapshot
    result = sim.simulate(msg("implementation", 2), now=NOW)
    assert result.outcome == "rejected" and "invalidated" in result.reason


@pytest.mark.parametrize("how", ["header", "truncated"])
def test_direct_gate_real_sqlite_corruption_fails_closed(tmp_path, how):
    gate, signers = direct_gate(tmp_path)
    assert gate.reserve(sign_direct(msg(), signers), NOW).outcome == "reserved"
    corrupt_store(tmp_path / "direct-gate.db", how)
    root, _ = make_test_keys()
    with pytest.raises(AdmissionError, match="storage failure"):
        OfflineAdmissionGate(tmp_path / "direct-gate.db", root, FixtureEvidence(), FixtureHalt())
    with pytest.raises(AdmissionError, match="storage failure"):
        gate.reserve(sign_direct(msg("implementation", 2), signers), NOW)


# ---- Direct gate: legacy object, absent fields, wrong actor, tamper, time, key ----

def test_direct_gate_rejects_authentic_legacy_envelope(tmp_path):
    gate, _ = direct_gate(tmp_path)
    for kind, seq in (("assignment", 1), ("implementation", 2), ("correction_request", 3)):
        legacy = legacy_envelope(kind, seq)
        assert type(legacy) is AuthenticatedEnvelope
        result = gate.reserve(legacy, NOW)
        assert result.outcome == "rejected" and result.reason == "malformed payload"
    assert count_rows(tmp_path / "direct-gate.db") == 0


@pytest.mark.parametrize("kind,remove,reason", [
    ("assignment", "base_sha", "invalid base_sha"),
    ("implementation", "implementation_sha", "invalid implementation_sha"),
    ("correction", "implementation_sha", "invalid implementation_sha"),
    ("review", "implementation_sha", "invalid implementation_sha"),
    ("assignment", "evidence_id", "missing or empty evidence_id"),
    ("implementation", "evidence_id", "missing or empty evidence_id"),
    ("review", "evidence_id", "missing or empty evidence_id"),
])
def test_direct_gate_rejects_absent_sha_and_evidence(tmp_path, kind, remove, reason):
    """Absent verdict is not a B2.1 gate field; it is covered at contract/simulator layers."""
    gate, signers = direct_gate(tmp_path)
    m = msg(kind, NEXT_SEQUENCE[kind])
    del m.payload[remove]
    result = gate.reserve(sign_direct(m, signers), NOW)
    assert result.outcome == "rejected" and reason in result.reason
    assert count_rows(tmp_path / "direct-gate.db") == 0


def test_direct_gate_rejects_wrong_sender_and_recipient(tmp_path):
    gate, signers = direct_gate(tmp_path)
    cases = [
        # wrong recipient, valid chief signature
        (sign_direct(msg(recipient="skippy"), signers), "unauthorized sender/role/recipient/type"),
        # wrong sender with its own valid key and role for an assignment
        (sign_direct(msg(sender="skippy", role="reviewer"), signers), "unauthorized sender/role/recipient/type"),
        # sender claims chief role but signs with implementer key entry
        (sign_direct(msg(sender="jason"), signers), "Role mismatch"),
        # sender chief, key entry belongs to jason
        (sign_direct(msg(), signers, key_id=signers["jason"][0], signing_key=signers["jason"][1]),
         "Actor mismatch"),
    ]
    for envelope, reason in cases:
        result = gate.reserve(envelope, NOW)
        assert result.outcome == "rejected" and reason in result.reason, result
    assert count_rows(tmp_path / "direct-gate.db") == 0


def test_direct_gate_rejects_signed_payload_and_field_tamper(tmp_path):
    gate, signers = direct_gate(tmp_path)
    envelope = sign_direct(msg(), signers)
    tampered = replace(envelope, payload={**envelope.payload, "base_sha": TAMPERED_SHA})
    restamped = replace(tampered, payload_digest=hashlib.sha256(_canonical_json(tampered.payload)).digest())
    field_tamper = replace(envelope, message_id="msg-tampered")
    for candidate, reason in ((tampered, "payload_digest does not match"),
                              (restamped, "Invalid signature"),
                              (field_tamper, "Invalid signature")):
        result = gate.reserve(candidate, NOW)
        assert result.outcome == "rejected" and reason in result.reason, result
    assert count_rows(tmp_path / "direct-gate.db") == 0
    assert gate.reserve(envelope, NOW).outcome == "reserved"
    assert count_rows(tmp_path / "direct-gate.db") == 1


@pytest.mark.parametrize("skew_seconds", [60, 61, 3600])
def test_direct_gate_rejects_future_timestamp(tmp_path, skew_seconds):
    gate, signers = direct_gate(tmp_path)
    timestamp = NOW + timedelta(seconds=skew_seconds)
    envelope = sign_direct(msg(timestamp=timestamp, expiry=timestamp + timedelta(hours=1)), signers)
    result = gate.reserve(envelope, NOW)
    assert result.outcome == "rejected" and "too far in the future" in result.reason
    assert count_rows(tmp_path / "direct-gate.db") == 0


def test_direct_gate_rejects_unknown_key_and_wrong_signing_key(tmp_path):
    gate, signers = direct_gate(tmp_path)
    unknown = sign_direct(msg(), signers, key_id=UNKNOWN_KEY_ID, signing_key=SigningKey(UNKNOWN_SEED))
    result = gate.reserve(unknown, NOW)
    assert result.outcome == "rejected" and "Unknown key_id" in result.reason
    wrong_signer = sign_direct(msg(), signers, signing_key=SigningKey(UNKNOWN_SEED))
    result = gate.reserve(wrong_signer, NOW)
    assert result.outcome == "rejected" and "Invalid signature" in result.reason
    assert count_rows(tmp_path / "direct-gate.db") == 0


# --- Work order 6845727928: single-active-transition (reentrancy) guard. ---
# Regression for finding 6845540923. One-shot fixture callbacks attempt a nested
# simulate() on the SAME thread while the outer call holds the simulator RLock.
# Every callback sets its one-shot flag BEFORE recursing, so recursion depth is
# at most one even without the guard, and every thread wait is finite. The
# guard is a test-fixture consistency control, not a sandbox against arbitrary
# in-process code (see the private-API bypass test below).

REENTRANT_REASON = "reentrant simulate rejected"


class RecursivePolicy(FixturePolicy):
    """One-shot fixture policy that attempts nested admission on the same thread."""

    def __init__(self, inner_message, *, kind, after="verified"):
        super().__init__()
        self.sim = None
        self.inner_message = inner_message
        self.kind = kind
        self.after = after  # verified | denied | raise
        self.inner_results = []
        self.fired = False

    def check(self, message, now):
        result = super().check(message, now)
        if not self.fired and message.payload.get("type") == self.kind:
            self.fired = True
            self.inner_results.append(self.sim.simulate(self.inner_message, now=now))
            if self.after == "denied":
                return "rejected"
            if self.after == "raise":
                raise RuntimeError("synthetic recursive policy failure")
        return result


class RecursiveHalt(FixtureHalt):
    """One-shot HALT probe that attempts nested admission from inside gate.reserve."""

    def __init__(self, inner_message):
        super().__init__()
        self.sim = None
        self.inner_message = inner_message
        self.inner_results = []
        self.fired = False

    def read(self):
        if self.sim is not None and not self.fired:
            self.fired = True
            self.inner_results.append(self.sim.simulate(self.inner_message, now=NOW))
        return super().read()


def count_plan_sign_and_gate(monkeypatch, sim):
    calls = {"plan": 0, "sign": 0, "reserve": 0}
    original_plan = simulator_module.plan_transition
    original_sign = sim._sign_message
    original_reserve = sim._gate.reserve

    def wrap_plan(*args, **kwargs):
        calls["plan"] += 1
        return original_plan(*args, **kwargs)

    def wrap_sign(message):
        calls["sign"] += 1
        return original_sign(message)

    def wrap_reserve(*args, **kwargs):
        calls["reserve"] += 1
        return original_reserve(*args, **kwargs)

    monkeypatch.setattr(simulator_module, "plan_transition", wrap_plan)
    monkeypatch.setattr(sim, "_sign_message", wrap_sign)
    monkeypatch.setattr(sim._gate, "reserve", wrap_reserve)
    return calls


def reference_snapshot(tmp_path, messages):
    """The same messages on a separate non-recursive simulator and fresh store."""
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    ref = simulator(ref_dir)
    for message in messages:
        assert ref.simulate(message, now=NOW).outcome == "reserved"
    return ref.snapshot


def reserved_identities(db):
    if not Path(db).exists():
        return []
    with closing(sqlite3.connect(db)) as con:
        return con.execute(
            "SELECT message_id, idempotency_key, sequence, state FROM reservations ORDER BY sequence"
        ).fetchall()


@pytest.mark.parametrize("kind,seq", [("assignment", 1), ("implementation", 2)])
def test_recursive_policy_nested_admission_rejected(tmp_path, monkeypatch, kind, seq):
    """Inner same-thread simulate is rejected before planning, signing or gate access.

    Defined behavior: the outer call continues with its own policy result. The
    rejected inner attempt changes no snapshot or reservation row, so the outer
    plan is still based on the current snapshot and commits exactly one row.
    """
    outer = msg(kind, seq)
    policy = RecursivePolicy(msg(kind, seq), kind=kind)
    sim = simulator(tmp_path, policy=policy)
    policy.sim = sim
    reserve_prerequisites(sim, kind)
    rows_before = reserved_identities(tmp_path / DB_NAME)
    snapshot_before = sim.snapshot
    calls = count_plan_sign_and_gate(monkeypatch, sim)
    result = sim.simulate(outer, now=NOW)
    assert len(policy.inner_results) == 1
    inner = policy.inner_results[0]
    assert inner.outcome == "rejected" and REENTRANT_REASON in inner.reason, inner
    assert result.outcome == "reserved", result
    assert calls == {"plan": 1, "sign": 1, "reserve": 1}, calls
    assert reserved_identities(tmp_path / DB_NAME) == rows_before + [
        (outer.message_id, outer.idempotency_key, seq, "RESERVED")]
    assert sim.snapshot == reference_snapshot(tmp_path, prerequisite_messages(kind) + [msg(kind, seq)])
    assert sim.snapshot.next_sequence == snapshot_before.next_sequence + 1
    assert sim.snapshot.receipts[-1] == (outer.message_id, outer.idempotency_key, _fingerprint(outer))
    assert sim._in_progress is False


def test_recursive_halt_probe_inside_gate_is_rejected(tmp_path, monkeypatch):
    """Nested admission from an injected gate dependency is rejected the same way."""
    halt = RecursiveHalt(msg())
    sim = simulator(tmp_path, halt=halt)
    halt.sim = sim
    calls = count_plan_sign_and_gate(monkeypatch, sim)
    result = sim.simulate(msg(), now=NOW)
    assert len(halt.inner_results) == 1
    inner = halt.inner_results[0]
    assert inner.outcome == "rejected" and REENTRANT_REASON in inner.reason, inner
    assert result.outcome == "reserved", result
    assert calls == {"plan": 1, "sign": 1, "reserve": 1}, calls
    assert reserved_identities(tmp_path / DB_NAME) == [
        (msg().message_id, msg().idempotency_key, 1, "RESERVED")]
    assert sim.snapshot == reference_snapshot(tmp_path, [msg()])
    assert sim._in_progress is False


@pytest.mark.parametrize("after,reason", [
    ("denied", "synthetic fixture policy denied"),
    ("raise", "synthetic signing/policy failed closed"),
])
def test_guard_resets_after_recursive_policy_denial_or_exception(tmp_path, monkeypatch, after, reason):
    policy = RecursivePolicy(msg(), kind="assignment", after=after)
    sim = simulator(tmp_path, policy=policy)
    policy.sim = sim
    calls = count_plan_sign_and_gate(monkeypatch, sim)
    result = sim.simulate(msg(), now=NOW)
    assert result.outcome == "rejected" and reason in result.reason, result
    inner = policy.inner_results[0]
    assert inner.outcome == "rejected" and REENTRANT_REASON in inner.reason, inner
    assert calls == {"plan": 1, "sign": 0, "reserve": 0}, calls
    assert sim.snapshot.stage == "EMPTY"
    assert reserved_identities(tmp_path / DB_NAME) == []
    assert sim._in_progress is False
    # Guard released on the failure path: the one-shot policy no longer recurses.
    assert sim.simulate(msg(), now=NOW).outcome == "reserved"
    assert calls == {"plan": 2, "sign": 1, "reserve": 1}, calls
    assert len(reserved_identities(tmp_path / DB_NAME)) == 1
    assert sim._in_progress is False


def test_guard_resets_after_normal_success_duplicate_and_rejection(tmp_path):
    sim = simulator(tmp_path)
    assert sim._in_progress is False
    for kind, seq in [("assignment", 1), ("implementation", 2), ("review", 3)]:
        assert sim.simulate(msg(kind, seq), now=NOW).outcome == "reserved"
        assert sim._in_progress is False
    assert sim.simulate(msg("review", 3), now=NOW).outcome == "duplicate"
    assert sim._in_progress is False
    assert sim.simulate(msg("implementation", 9), now=NOW).outcome == "rejected"
    assert sim._in_progress is False
    assert sim.simulate(msg(), now=datetime(2026, 1, 1)).outcome == "rejected"
    assert sim._in_progress is False


def test_guard_resets_after_storage_exception_and_session_stays_failstop(tmp_path, monkeypatch):
    sim = simulator(tmp_path)
    reserve_calls = []

    def fail(*args, **kwargs):
        reserve_calls.append(True)
        raise AdmissionError("indeterminate commit")

    monkeypatch.setattr(sim._gate, "reserve", fail)
    with pytest.raises(AdmissionError, match="indeterminate"):
        sim.simulate(msg(), now=NOW)
    assert sim._in_progress is False
    result = sim.simulate(msg(), now=NOW)
    assert result.outcome == "rejected" and "invalidated" in result.reason, result
    assert reserve_calls == [True]
    assert sim.snapshot.stage == "EMPTY"


def test_private_api_bypass_is_detected_not_sandboxed(tmp_path, monkeypatch):
    """The guard is not a sandbox: a callback calling the private locked method
    can still reserve. The outer call detects the snapshot change after policy,
    rejects before signing/reserve and invalidates the session (fail closed)."""

    class BypassPolicy(FixturePolicy):
        def __init__(self):
            super().__init__()
            self.sim = None
            self.inner_results = []
            self.fired = False

        def check(self, message, now):
            if not self.fired:
                self.fired = True
                self.inner_results.append(self.sim._simulate_locked(msg(), now=now))
            return super().check(message, now)

    policy = BypassPolicy()
    sim = simulator(tmp_path, policy=policy)
    policy.sim = sim
    outer = msg(message_id="msg-outer", idempotency_key="idem-outer")
    calls = count_plan_sign_and_gate(monkeypatch, sim)
    result = sim.simulate(outer, now=NOW)
    assert policy.inner_results[0].outcome == "reserved"
    assert result.outcome == "rejected" and "snapshot changed" in result.reason, result
    assert calls == {"plan": 2, "sign": 1, "reserve": 1}, calls
    assert [row[0] for row in reserved_identities(tmp_path / DB_NAME)] == [msg().message_id]
    assert sim._in_progress is False
    follow_up = sim.simulate(msg("implementation", 2), now=NOW)
    assert follow_up.outcome == "rejected" and "invalidated" in follow_up.reason, follow_up


class BlockingPolicy(FixturePolicy):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocked_once = False

    def check(self, message, now):
        result = super().check(message, now)
        if not self.blocked_once:
            self.blocked_once = True
            self.entered.set()
            if not self.release.wait(timeout=10):
                return "rejected"
        return result


def test_cross_thread_call_waits_for_active_transition_not_rejected(tmp_path):
    """Same-instance threads (one simulator, one RLock, ONE process); not process evidence.

    Diagnostics tightened by work order 6846218272. ``second_entering`` is set
    immediately before the contender calls simulate(): it shows the contender
    thread reached the call site while the first transition was held open in the
    fixture policy. It is NOT proof that the contender is blocked inside the RLock
    acquire. The bounded 0.3 s check only observes that the contender had not
    completed while the first transition was held; it is not lock-contention
    proof. The conclusive assertions are the outcomes: both calls reserve and the
    contender is never rejected as reentrant. Only started threads are joined, and
    every wait and join is finite.
    """
    policy = BlockingPolicy()
    sim = simulator(tmp_path, policy=policy)
    outcomes, errors = {}, []
    second_entering = threading.Event()
    second_done = threading.Event()

    def run(name, message, entering=None, done=None):
        try:
            if entering is not None:
                entering.set()
            outcomes[name] = sim.simulate(message, now=NOW)
        except BaseException as exc:  # noqa: BLE001 - reported via assertion below
            errors.append((name, repr(exc)))
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=run, args=("first", msg()), name="b2b2-first")
    second = threading.Thread(
        target=run, args=("second", msg("implementation", 2), second_entering, second_done),
        name="b2b2-contender")
    started = []
    early_completion = None
    try:
        first.start()
        started.append(first)
        assert policy.entered.wait(timeout=10), "first transition never reached the fixture policy"
        second.start()
        started.append(second)
        assert second_entering.wait(timeout=10), "contender thread never reached simulate()"
        early_completion = second_done.wait(timeout=0.3)
    finally:
        policy.release.set()
        for thread in started:
            thread.join(timeout=10)
    alive = [thread.name for thread in started if thread.is_alive()]
    assert alive == [], f"threads still alive after finite join: {alive}"
    assert errors == [], errors
    assert early_completion is False, (
        "contender completed while the first transition was held open: "
        f"{outcomes.get('second')!r}")
    assert outcomes["first"].outcome == "reserved", outcomes
    assert outcomes["second"].outcome == "reserved", outcomes
    assert REENTRANT_REASON not in outcomes["second"].reason
    assert len(reserved_identities(tmp_path / DB_NAME)) == 2
    assert sim.snapshot.next_sequence == 3
    assert sim._in_progress is False


def test_private_api_bypass_during_reserve_fails_closed(tmp_path):
    """A gate dependency calling the private locked method changes the snapshot
    mid-reserve; the outer call refuses to apply its plan and invalidates."""

    class BypassHalt(FixtureHalt):
        def __init__(self):
            super().__init__()
            self.sim = None
            self.inner_results = []
            self.fired = False

        def read(self):
            if self.sim is not None and not self.fired:
                self.fired = True
                self.inner_results.append(self.sim._simulate_locked(msg(), now=NOW))
            return super().read()

    halt = BypassHalt()
    sim = simulator(tmp_path, halt=halt)
    halt.sim = sim
    with pytest.raises(AdmissionError, match="snapshot changed during reservation"):
        sim.simulate(msg(), now=NOW)
    assert halt.inner_results[0].outcome == "reserved"
    assert reserved_identities(tmp_path / DB_NAME) == [
        (msg().message_id, msg().idempotency_key, 1, "RESERVED")]
    assert sim._in_progress is False
    follow_up = sim.simulate(msg("implementation", 2), now=NOW)
    assert follow_up.outcome == "rejected" and "invalidated" in follow_up.reason, follow_up


# --- Work order 6846218272 item 1: T25 durable identity namespace (direct gate). ---
# Direct-gate layer only: the authentic OfflineAdmissionGate and B1 verify_signature
# over test-signed SignedEnvelope values. No simulator and no contract stream
# binding is involved, so the simulator's checkpoint-mismatch rejection cannot be
# the cause of any result here. Checkpoint A (assignment sequence 1 and
# implementation sequence 2) is reserved in a disposable store; a NEW gate
# instance is then constructed against that same store (read-only preflight plus
# locked revalidation of the existing schema). Otherwise valid checkpoint-B
# sequence-1 assignments in the SAME project/task reuse checkpoint-A identities.
# The checkpoint-B signed payload equals checkpoint-A row 1's payload, so case (c)
# differs from that row only in checkpoint_id. Complete rows (every column,
# including id, payload_digest and recorded_at) are compared before and after.

T25_DB = "t25-direct-gate.db"
T25_A1 = ("t25-A-msg-1", "t25-A-idem-1")
T25_A2 = ("t25-A-msg-2", "t25-A-idem-2")
T25_CONFLICTS = {
    "a_reused_message_id_new_idempotency_key": (
        (T25_A1[0], "t25-B-idem-new"), "conflicting identity"),
    "b_new_message_id_reused_idempotency_key": (
        ("t25-B-msg-new", T25_A1[1]), "conflicting identity"),
    "c_both_reused_same_row": (T25_A1, "conflicting identity"),
    "d_cross_matched_a1_message_a2_key": (
        (T25_A1[0], T25_A2[1]), "ambiguous or conflicting identity matches"),
    "d_cross_matched_a2_message_a1_key": (
        (T25_A2[0], T25_A1[1]), "ambiguous or conflicting identity matches"),
}


def t25_message(kind, seq, message_id, idempotency_key, **fields):
    data = dict(project_id="QLA", task_id="task-test", checkpoint_id="ckpt-A",
                message_id=message_id, idempotency_key=idempotency_key)
    data.update(fields)
    return msg(kind, seq, **data)


def t25_checkpoint_b(message_id, idempotency_key, **fields):
    """Otherwise valid checkpoint-B sequence-1 assignment (same signed payload as A1)."""
    return t25_message("assignment", 1, message_id, idempotency_key,
                       checkpoint_id="ckpt-B", **fields)


def full_reservation_rows(db):
    """Every column of every reservation row, in id order."""
    with closing(sqlite3.connect(db)) as con:
        return con.execute("SELECT * FROM reservations ORDER BY id").fetchall()


def t25_store_with_checkpoint_a(tmp_path):
    db = tmp_path / T25_DB
    root, signers = make_test_keys()
    first_gate = OfflineAdmissionGate(db, root, FixtureEvidence(), FixtureHalt())
    for message in (t25_message("assignment", 1, *T25_A1),
                    t25_message("implementation", 2, *T25_A2)):
        result = first_gate.reserve(sign_direct(message, signers), NOW)
        assert (result.outcome, result.reason) == ("reserved", "ok"), result
    del first_gate
    rebuilt_root, _ = make_test_keys()
    gate = OfflineAdmissionGate(db, rebuilt_root, FixtureEvidence(), FixtureHalt())
    rows = full_reservation_rows(db)
    assert [(r[1], r[2], r[3], r[5], r[6], r[11], r[12]) for r in rows] == [
        ("QLA", "task-test", "ckpt-A", *T25_A1, 1, "RESERVED"),
        ("QLA", "task-test", "ckpt-A", *T25_A2, 2, "RESERVED"),
    ]
    return db, gate, signers, rows


@pytest.mark.parametrize("case", sorted(T25_CONFLICTS))
def test_t25_direct_gate_cross_checkpoint_identity_reuse_rejected(tmp_path, case):
    """Durable project+task identity namespace across checkpoints (direct gate only)."""
    db, gate, signers, rows_before = t25_store_with_checkpoint_a(tmp_path)
    (message_id, idempotency_key), expected_reason = T25_CONFLICTS[case]
    result = gate.reserve(sign_direct(t25_checkpoint_b(message_id, idempotency_key), signers), NOW)
    assert result.outcome != "duplicate", result
    assert (result.outcome, result.reason) == ("rejected", expected_reason), result
    assert full_reservation_rows(db) == rows_before
    # Fresh-ID control on the SAME reconstructed gate and store: same project, task,
    # checkpoint B, sequence 1 and signed payload. It reserves, so checkpoint
    # mismatch, sequence, signature, evidence or HALT did not cause the rejection.
    control = t25_checkpoint_b("t25-B-msg-fresh", "t25-B-idem-fresh")
    result = gate.reserve(sign_direct(control, signers), NOW)
    assert (result.outcome, result.reason) == ("reserved", "ok"), result
    rows_after = full_reservation_rows(db)
    assert rows_after[:2] == rows_before
    assert [(r[1], r[2], r[3], r[5], r[6], r[11], r[12]) for r in rows_after[2:]] == [
        ("QLA", "task-test", "ckpt-B", "t25-B-msg-fresh", "t25-B-idem-fresh", 1, "RESERVED")]


@pytest.mark.parametrize("boundary", ["other_project", "other_task"])
def test_t25_direct_gate_identity_namespace_is_project_and_task(tmp_path, boundary):
    """Independent project/task control: reused checkpoint-A IDs reserve in another
    namespace, and an exact redelivery there is a duplicate, never a new row."""
    db, gate, signers, rows_before = t25_store_with_checkpoint_a(tmp_path)
    scope = {"other_project": {"project_id": "QLA-t25-other"},
             "other_task": {"task_id": "task-t25-other"}}[boundary]
    envelope = sign_direct(t25_checkpoint_b(*T25_A1, **scope), signers)
    result = gate.reserve(envelope, NOW)
    assert (result.outcome, result.reason) == ("reserved", "ok"), result
    rows_after = full_reservation_rows(db)
    assert rows_after[:2] == rows_before and len(rows_after) == 3
    project, task = scope.get("project_id", "QLA"), scope.get("task_id", "task-test")
    assert (rows_after[2][1], rows_after[2][2], rows_after[2][3],
            rows_after[2][5], rows_after[2][6]) == (project, task, "ckpt-B", *T25_A1)
    result = gate.reserve(envelope, NOW)
    assert (result.outcome, result.reason) == ("duplicate", "identical reservation"), result
    assert full_reservation_rows(db) == rows_after


# --- Work order 6846218272 item 2: separate-process contention (direct gate). ---
# Two spawned child processes (multiprocessing "spawn", the Windows default) each
# build their own trust root, signers, OfflineAdmissionGate and SQLite connections
# over ONE store the parent pre-initialized. No concurrent fresh-store creation is
# claimed. Parent monkeypatches are not inherited by spawn, so every child
# installs the named zero-invocation sentinels itself and returns the counts. The
# worker is module-level in this test module; spawn re-imports the module by name
# using the parent's sys.path. Bounded fresh-store repetitions, no retries; this
# is not exhaustive scheduling, crash or simulator cross-process proof.

PROCESS_DB = "process-contention.db"
PROCESS_READY_TIMEOUT = 120.0
PROCESS_RELEASE_TIMEOUT = 60.0
PROCESS_RESULT_TIMEOUT = 120.0
PROCESS_JOIN_TIMEOUT = 30.0
PROCESS_REPETITIONS = 3
ZERO_SENTINELS = {"ledger.record_event": 0, "transport.__call__": 0,
                  "urllib.request.urlopen": 0, "place_limit_order": 0}


def _process_assignment(name):
    """Two distinct valid same-checkpoint sequence-1 assignments (distinct IDs and payloads)."""
    message = msg("assignment", 1, message_id=f"proc-msg-{name}",
                  idempotency_key=f"proc-idem-{name}")
    if name == "second":
        message.payload.update(evidence_id="fixture-proc-second", base_sha=IMPLEMENTATION)
    return message


def _process_contention_worker(db_path, name, conn, release, release_timeout):
    """Spawned child. Reports PID and ready only after its own initialization, waits
    on one bounded release, makes exactly one reserve call and always returns a
    typed outcome or the full formatted traceback (never re-raises)."""
    report = {"name": name, "pid": os.getpid(), "ppid": os.getppid(), "outcome": None,
              "reason": None, "error": None, "sentinels": None}
    try:
        with pytest.MonkeyPatch.context() as patcher:
            counts = install_protected_entry_sentinels(patcher)
            try:
                root, signers = make_test_keys()
                gate = OfflineAdmissionGate(Path(db_path), root, FixtureEvidence(), FixtureHalt())
                envelope = sign_direct(_process_assignment(name), signers)
                conn.send(("ready", name, os.getpid()))
                if not release.wait(timeout=release_timeout):
                    report["error"] = f"release not received within {release_timeout}s"
                else:
                    result = gate.reserve(envelope, NOW)
                    report["outcome"], report["reason"] = result.outcome, result.reason
            finally:
                report["sentinels"] = dict(counts)
    except BaseException:  # noqa: BLE001 - full traceback is returned to the parent
        report["error"] = traceback.format_exc()
    try:
        conn.send(("result", report))
    finally:
        conn.close()


def _stop_children(children):
    """Terminate (kill fallback) and join every started, still-live child."""
    forced, leftovers = [], []
    for name, process in children.items():
        if process.is_alive():
            forced.append(name)
            process.terminate()
            process.join(PROCESS_JOIN_TIMEOUT)
            if process.is_alive():
                process.kill()
                process.join(PROCESS_JOIN_TIMEOUT)
        if process.is_alive():
            leftovers.append((name, process.pid))
    return forced, leftovers


def run_process_contention(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    db = tmp_path / PROCESS_DB
    root, _ = make_test_keys()
    OfflineAdmissionGate(db, root, FixtureEvidence(), FixtureHalt())  # schema v2 + WAL, once
    assert full_reservation_rows(db) == []
    release = ctx.Event()
    children, receivers, senders = {}, {}, []
    evidence = {"start_method": ctx.get_start_method(), "parent_pid": os.getpid()}
    in_flight = None
    try:
        for name in ("first", "second"):
            receiver, sender = ctx.Pipe(duplex=False)
            receivers[name] = receiver
            senders.append(sender)
            process = ctx.Process(
                target=_process_contention_worker,
                args=(str(db), name, sender, release, PROCESS_RELEASE_TIMEOUT),
                name=f"b2b2-contender-{name}", daemon=True)
            children[name] = process
            process.start()
            sender.close()  # parent keeps only the receiving end
        ready = {}
        for name, receiver in receivers.items():
            assert receiver.poll(PROCESS_READY_TIMEOUT), (
                f"{name} child not ready within {PROCESS_READY_TIMEOUT}s "
                f"(exitcode={children[name].exitcode})")
            message = receiver.recv()
            assert message[0] == "ready" and message[1] == name, (
                f"{name} child failed before ready: {message!r}")
            ready[name] = message[2]
        assert ready == {name: process.pid for name, process in children.items()}, ready
        assert len(set(ready.values())) == 2 and os.getpid() not in ready.values(), ready
        release.set()  # exactly once
        reports = {}
        for name, receiver in receivers.items():
            assert receiver.poll(PROCESS_RESULT_TIMEOUT), (
                f"{name} child returned no result within {PROCESS_RESULT_TIMEOUT}s")
            kind, report = receiver.recv()
            assert kind == "result", (kind, report)
            reports[name] = report
        for name, process in children.items():
            process.join(PROCESS_JOIN_TIMEOUT)
            assert not process.is_alive(), f"{name} child still alive after finite join"
        evidence["exitcodes"] = {name: process.exitcode for name, process in children.items()}
        evidence["children"] = reports
        for name, report in reports.items():
            assert report["error"] is None, f"{name} child error:\n{report['error']}"
            assert report["pid"] == ready[name] and report["ppid"] == os.getpid(), report
            assert report["sentinels"] == ZERO_SENTINELS, report
        assert evidence["exitcodes"] == {"first": 0, "second": 0}, evidence["exitcodes"]
        winners = [name for name, report in reports.items() if report["outcome"] == "reserved"]
        assert len(winners) == 1, reports
        winner = winners[0]
        loser = "second" if winner == "first" else "first"
        assert reports[winner]["reason"] == "ok", reports
        assert (reports[loser]["outcome"], reports[loser]["reason"]) == (
            "rejected", "sequence 1 != 2"), reports
        expected = _process_assignment(winner)
        rows = full_reservation_rows(db)
        assert [row[1:] for row in rows] == [(
            expected.project_id, expected.task_id, expected.checkpoint_id,
            expected.assignment_id, expected.message_id, expected.idempotency_key,
            expected.sender, expected.recipient, expected.role,
            hashlib.sha256(_canonical_json(expected.payload)).digest(),
            1, "RESERVED", NOW.isoformat(),
        )], rows
        evidence["winner"], evidence["committed_message_id"] = winner, rows[0][5]
    except BaseException as exc:
        in_flight = exc
        raise
    finally:
        forced, leftovers = _stop_children(children)
        for conn in list(receivers.values()) + senders:
            conn.close()
        evidence["forced_cleanup"], evidence["leftovers"] = forced, leftovers
        if in_flight is None and (forced or leftovers):
            raise AssertionError(f"child cleanup required: forced={forced} leftovers={leftovers}")
    return evidence


@pytest.mark.parametrize("repetition", range(PROCESS_REPETITIONS))
def test_separate_process_contention_one_reserved_one_sequence_rejected(tmp_path, repetition):
    evidence = run_process_contention(tmp_path)
    evidence["repetition"] = repetition
    (tmp_path / "process-contention-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    assert evidence["start_method"] == "spawn"
    assert evidence["forced_cleanup"] == [] and evidence["leftovers"] == []


# --- Work order 6846823630 item 1: CHARACTERIZATION of CURRENT invalidation behavior. ---
# Finding 6846379683. These tests RECORD the current, unresolved behavior of
# invalidate_for_restart() over the authentic B2.1 OfflineAdmissionGate. They are
# characterization tests: a pass proves only that the observed interleaving
# reproduces. It is NOT a safety property, NOT a fix and NOT the proposed
# serialized-close semantics in the design doc. At this source
# invalidate_for_restart() writes the flag without taking the simulator lock, and
# simulate() reads the flag once, at the start of _simulate_locked, so an
# invalidation that lands after that read does not stop the active call. The
# expectations below were first observed on authentic Windows and then asserted.
# Coordination uses finite Events and finite joins only; no ordering relies on
# sleep. Only started threads are joined. Worker exceptions are captured.

CHAR_WAIT = 10.0
CHAR_RESERVED_REASON = "synthetic reservation only; no ledger append"
CHAR_INVALIDATED_REASON = "simulation invalidated; no crash/restart continuation"
CHAR_POINTS = ("policy", "halt", "evidence")
# Calls already made when the callback pauses or invalidates. The fixture policy
# runs before signing (pre-sign). The HALT probe and evidence verifier run inside
# the authentic gate.reserve, after signing and before the gate's locked
# BEGIN IMMEDIATE / INSERT (in-reserve).
CHAR_CALLS_AT_POINT = {
    "policy": {"plan": 1, "sign": 0, "reserve": 0},
    "halt": {"plan": 1, "sign": 1, "reserve": 1},
    "evidence": {"plan": 1, "sign": 1, "reserve": 1},
}
CHAR_ONE_CALL_EACH = {"plan": 1, "sign": 1, "reserve": 1}


class _OneShotPause:
    """Pauses the first callback invocation on finite coordination events."""

    def _init_pause(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fired = False
        self.released_in_time = None

    def _pause(self):
        if self.fired:
            return True
        self.fired = True
        self.entered.set()
        self.released_in_time = self.release.wait(timeout=CHAR_WAIT)
        return self.released_in_time


class PausingPolicy(FixturePolicy, _OneShotPause):
    def __init__(self):
        super().__init__()
        self._init_pause()

    def check(self, message, now):
        result = super().check(message, now)
        return result if self._pause() else "rejected"


class PausingHalt(FixtureHalt, _OneShotPause):
    def __init__(self):
        super().__init__()
        self._init_pause()

    def read(self):
        state = super().read()
        return state if self._pause() else "halted"


class PausingEvidence(FixtureEvidence, _OneShotPause):
    def __init__(self):
        super().__init__()
        self._init_pause()

    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        verdict = super().verify(kind, assignment_id, bound_sha, evidence_id, now)
        return verdict if self._pause() else "rejected"


class _OneShotInvalidate:
    """Calls sim.invalidate_for_restart() once, on the simulate() thread."""

    def _init_invalidate(self):
        self.sim = None
        self.calls = None
        self.fired = False
        self.returned = []
        self.flag_after = None
        self.in_progress_at = None
        self.calls_at = None

    def _invalidate(self):
        if self.sim is None or self.fired:
            return
        self.fired = True
        self.in_progress_at = self.sim._in_progress
        self.calls_at = dict(self.calls)
        self.returned.append(self.sim.invalidate_for_restart())
        self.flag_after = self.sim._invalidated


class InvalidatingPolicy(FixturePolicy, _OneShotInvalidate):
    def __init__(self):
        super().__init__()
        self._init_invalidate()

    def check(self, message, now):
        self._invalidate()
        return super().check(message, now)


class InvalidatingHalt(FixtureHalt, _OneShotInvalidate):
    def __init__(self):
        super().__init__()
        self._init_invalidate()

    def read(self):
        self._invalidate()
        return super().read()


class InvalidatingEvidence(FixtureEvidence, _OneShotInvalidate):
    def __init__(self):
        super().__init__()
        self._init_invalidate()

    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        self._invalidate()
        return super().verify(kind, assignment_id, bound_sha, evidence_id, now)


CHAR_PAUSING = {"policy": PausingPolicy, "halt": PausingHalt, "evidence": PausingEvidence}
CHAR_INVALIDATING = {
    "policy": InvalidatingPolicy, "halt": InvalidatingHalt, "evidence": InvalidatingEvidence}


def characterization_reference(tmp_path, messages):
    """The same messages on a separate, never-invalidated simulator and fresh store."""
    ref_dir = tmp_path / "characterization-reference"
    ref_dir.mkdir()
    ref = simulator(ref_dir)
    results = [(r.outcome, r.reason) for r in (ref.simulate(m, now=NOW) for m in messages)]
    return results, full_reservation_rows(ref_dir / DB_NAME), ref.snapshot


def _char_json(value):
    return value.hex() if isinstance(value, bytes) else repr(value)


def _char_finish(obs, tmp_path, sim, calls, outer):
    """Record the outer result, full rows, snapshot, counts and subsequent-call denial."""
    db = tmp_path / DB_NAME
    obs["outer"] = None if outer is None else (outer.outcome, outer.reason)
    obs["calls_after_outer"] = dict(calls)
    obs["rows_after_outer"] = full_reservation_rows(db)
    obs["snapshot_after_outer"] = sim.snapshot
    obs["in_progress_after_outer"] = sim._in_progress
    obs["invalidated_flag_after_outer"] = sim._invalidated
    obs["follow_up"] = None
    if not obs["alive"]:
        follow = sim.simulate(msg("implementation", 2), now=NOW)
        obs["follow_up"] = (follow.outcome, follow.reason)
    obs["calls_after_follow_up"] = dict(calls)
    obs["rows_after_follow_up"] = full_reservation_rows(db)
    obs["snapshot_after_follow_up"] = sim.snapshot
    ref_results, ref_rows, ref_snapshot = characterization_reference(tmp_path, [msg()])
    obs["reference_results"] = ref_results
    obs["reference_rows"] = ref_rows
    obs["reference_snapshot"] = ref_snapshot
    (tmp_path / f"characterization-{obs['mode']}-{obs['point']}.json").write_text(
        json.dumps(obs, indent=2, sort_keys=True, default=_char_json), encoding="utf-8")
    return obs


def observe_cross_thread_invalidation(tmp_path, monkeypatch, point):
    """Thread A pauses in a callback; thread B invalidates and signals its return
    BEFORE the parent releases A. Records what currently happens."""
    callback = CHAR_PAUSING[point]()
    sim = simulator(tmp_path, **{point: callback})
    calls = count_plan_sign_and_gate(monkeypatch, sim)
    results, errors = {}, []
    invalidate_returned = threading.Event()

    def run_simulate():
        try:
            results["simulate"] = sim.simulate(msg(), now=NOW)
        except BaseException:  # noqa: BLE001 - reported through the observation
            errors.append(("simulate", traceback.format_exc()))

    def run_invalidate():
        try:
            results["invalidate_returned"] = sim.invalidate_for_restart()
            results["flag_after_invalidate"] = sim._invalidated
            invalidate_returned.set()
        except BaseException:  # noqa: BLE001 - reported through the observation
            errors.append(("invalidate", traceback.format_exc()))

    worker = threading.Thread(target=run_simulate, name=f"b2b2-char-{point}-simulate")
    invalidator = threading.Thread(target=run_invalidate, name=f"b2b2-char-{point}-invalidate")
    obs = {"mode": "cross_thread", "point": point, "paused": False,
           "invalidate_returned_before_release": False}
    started = []
    try:
        worker.start()
        started.append(worker)
        obs["paused"] = callback.entered.wait(timeout=CHAR_WAIT)
        if obs["paused"]:
            invalidator.start()
            started.append(invalidator)
            obs["invalidate_returned_before_release"] = invalidate_returned.wait(timeout=CHAR_WAIT)
            obs["calls_at_invalidate"] = dict(calls)
            obs["rows_at_invalidate"] = full_reservation_rows(tmp_path / DB_NAME)
            obs["in_progress_at_invalidate"] = sim._in_progress
            obs["simulate_alive_at_release"] = worker.is_alive()
    finally:
        callback.release.set()
        for thread in started:
            thread.join(timeout=CHAR_WAIT)
    obs["alive"] = [thread.name for thread in started if thread.is_alive()]
    obs["errors"] = errors
    obs["released_in_time"] = callback.released_in_time
    obs["invalidate_returned"] = (
        [results["invalidate_returned"]] if "invalidate_returned" in results else [])
    obs["flag_after_invalidate"] = results.get("flag_after_invalidate")
    return _char_finish(obs, tmp_path, sim, calls, results.get("simulate"))


def observe_same_thread_invalidation(tmp_path, monkeypatch, point):
    """A one-shot callback invalidates on the simulate() thread. Records what currently happens."""
    callback = CHAR_INVALIDATING[point]()
    sim = simulator(tmp_path, **{point: callback})
    calls = count_plan_sign_and_gate(monkeypatch, sim)
    callback.sim, callback.calls = sim, calls
    obs = {"mode": "same_thread", "point": point, "alive": [], "errors": []}
    outer = None
    try:
        outer = sim.simulate(msg(), now=NOW)
    except BaseException:  # noqa: BLE001 - reported through the observation
        obs["errors"].append(("simulate", traceback.format_exc()))
    obs["fired"] = callback.fired
    obs["invalidate_returned"] = list(callback.returned)
    obs["flag_after_invalidate"] = callback.flag_after
    obs["in_progress_at_invalidate"] = callback.in_progress_at
    obs["calls_at_invalidate"] = callback.calls_at
    return _char_finish(obs, tmp_path, sim, calls, outer)

def _assert_current_invalidation_race_reproduced(obs, point):
    """CURRENT behavior as first observed on authentic Windows (characterization, not safety)."""
    assert obs["alive"] == [] and obs["errors"] == [], obs
    assert obs["invalidate_returned"] == [None], obs
    assert obs["flag_after_invalidate"] is True, obs
    assert obs["in_progress_at_invalidate"] is True, obs
    assert obs["calls_at_invalidate"] == CHAR_CALLS_AT_POINT[point], obs
    # The active call is NOT stopped: it signs, reserves and advances the snapshot.
    assert obs["outer"] == ("reserved", CHAR_RESERVED_REASON), obs
    assert obs["calls_after_outer"] == CHAR_ONE_CALL_EACH, obs
    rows = obs["rows_after_outer"]
    assert len(rows) == 1 and rows == obs["reference_rows"], (rows, obs["reference_rows"])
    assert rows[0][5:7] + rows[0][11:14] == (
        msg().message_id, msg().idempotency_key, 1, "RESERVED", NOW.isoformat()), rows
    assert obs["reference_results"] == [("reserved", CHAR_RESERVED_REASON)], obs
    assert obs["snapshot_after_outer"] == obs["reference_snapshot"], obs
    assert obs["snapshot_after_outer"].stage == "AWAIT_IMPLEMENTATION", obs
    assert obs["invalidated_flag_after_outer"] is True, obs
    assert obs["in_progress_after_outer"] is False, obs
    # Only LATER public calls are denied, before planning, signing or reserve.
    assert obs["follow_up"] == ("rejected", CHAR_INVALIDATED_REASON), obs
    assert obs["calls_after_follow_up"] == CHAR_ONE_CALL_EACH, obs
    assert obs["rows_after_follow_up"] == rows, obs
    assert obs["snapshot_after_follow_up"] == obs["snapshot_after_outer"], obs


@pytest.mark.parametrize("point", CHAR_POINTS)
def test_characterization_cross_thread_invalidate_during_paused_callback_does_not_stop_active_call(
        tmp_path, monkeypatch, protected_entry_sentinels, point):
    """CHARACTERIZATION of current unresolved behavior (finding 6846379683), NOT safety.

    (a) point=policy: pre-sign pause in the fixture policy. (b) point=halt or
    point=evidence: in-reserve pause in an authentic gate callback, after signing
    and before the gate's locked insert. Thread B's invalidate_for_restart()
    returns None while thread A is paused and before any reservation row exists;
    after release thread A still reserves. A pass reproduces the race; it is not
    the proposed serialized-close semantics and not a fix.
    """
    obs = observe_cross_thread_invalidation(tmp_path, monkeypatch, point)
    assert obs["paused"] is True, "callback never paused within the finite wait"
    assert obs["invalidate_returned_before_release"] is True, obs
    assert obs["released_in_time"] is True, obs
    assert obs["simulate_alive_at_release"] is True, obs
    assert obs["rows_at_invalidate"] == [], obs
    _assert_current_invalidation_race_reproduced(obs, point)
    assert all(value == 0 for value in protected_entry_sentinels.values()), protected_entry_sentinels


@pytest.mark.parametrize("point", CHAR_POINTS)
def test_characterization_same_thread_callback_invalidate_does_not_stop_active_call(
        tmp_path, monkeypatch, protected_entry_sentinels, point):
    """CHARACTERIZATION of current unresolved behavior (finding 6846379683), NOT safety.

    (c) point=policy: a one-shot fixture policy invalidates on the simulate()
    thread before signing. (d) point=halt or point=evidence: a one-shot authentic
    gate callback invalidates on the same thread inside reserve. The RLock is
    already held by that thread, so nothing refuses or defers the invalidation and
    the active call still reserves. A pass reproduces current behavior only.
    """
    obs = observe_same_thread_invalidation(tmp_path, monkeypatch, point)
    assert obs["fired"] is True, obs
    _assert_current_invalidation_race_reproduced(obs, point)
    assert all(value == 0 for value in protected_entry_sentinels.values()), protected_entry_sentinels