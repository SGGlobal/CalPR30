"""BRIDGE-02B.2.2 offline-only integration with the existing B2.1 reservation gate.

No BRIDGE-02A ledger, network transport, production keys, or trading involved.
"""

import ast
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from skippy.collaboration.offline_admission_gate import AdmissionError, OfflineAdmissionGate
from skippy.collaboration.offline_signature_verifier import (
    KeyEntry, SignedEnvelope, load_trust_root,
)
from skippy.collaboration.offline_bridge02b2_contract import SyntheticContractError
from skippy.collaboration.offline_bridge02b2_simulator import SyntheticBridgeSimulator
from test_offline_bridge02b2_contract import NOW, BASE, IMPLEMENTATION, CORRECTION, msg


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


def test_complete_workflow_reserves_only(tmp_path):
    sim = simulator(tmp_path)
    for kind, seq in [("assignment", 1), ("implementation", 2), ("review", 3)]:
        assert sim.simulate(msg(kind, seq), now=NOW).outcome == "reserved"
    assert sim.snapshot.stage == "CLOSED"
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        assert con.execute("SELECT COUNT(*) FROM reservations WHERE state='RESERVED'").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='intent_journal'").fetchone()[0] == 0


def test_review_correction_workflow(tmp_path):
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
    sim = simulator(tmp_path)
    barrier = threading.Barrier(2)
    outcomes = []
    def attempt():
        barrier.wait()
        try:
            outcomes.append(sim.simulate(msg(), now=NOW).outcome)
        except AdmissionError:
            outcomes.append("failclosed")
    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert outcomes.count("reserved") == 1
    assert set(outcomes) <= {"reserved", "duplicate", "failclosed"}
    with sqlite3.connect(tmp_path / "synthetic-reservations.db") as con:
        assert con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1


def test_no_protected_adapter_or_transport_imports_in_simulator():
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
