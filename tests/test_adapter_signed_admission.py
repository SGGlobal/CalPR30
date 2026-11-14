"""BRIDGE-02B.2.2 J6 offline disposable-store prototype tests (work order 6868633069).

OFFLINE SYNTHETIC ONLY. Every store is a fresh tmp_path SQLite file provisioned
through ``provision_signed_receipts_store``; keys are deterministic test seeds
with 'test-b2b2-' key IDs; trust, evidence and HALT are injected fixtures.
execution_enabled=False and transport stays disabled. No deployed or
main-runner state, production key or production evidence is read or written.

Governing acceptance text: J5 M1-M10 and RF of
docs/bridge-02b2-compatibility-contract.md at 4d05c321.

Seams and their honest scope:
- Receipts commit faults use the private P-6 factory ``_receipts_connect``,
  wrapped per adapter instance by the test-local ``ScriptedConnection``:
  (a) failure raised after the target statement and before ``commit()`` is
  invoked, then a real rollback; RF the same, then ``rollback()`` raises;
  (b) ``commit()`` invoked, wrapper raises without delegating, then
  rollback; (c) the real ``commit()`` completes, then the wrapper raises.
- Gate and ledger faults are WHOLE-CALL wrappers around ``gate.reserve`` and
  ``ledger.record_event`` only. The internal SQLite commit seams inside the
  gate (GATE:521) and the ledger (``record_event``) are BLOCKED: they are not
  faulted, not approximated and not claimed by any test here.
- Fresh-process observations and independent participants run in
  ``multiprocessing`` spawn children with new connections. Every barrier wait
  is bounded (10 s); child start-up readiness is bounded separately (60 s).
  Children are released, terminated and joined in ``finally`` and no child
  may be alive at teardown. An inconclusive (timed-out) schedule fails.
- Classifications asserted are the caller's own observation (J2). No test
  infers caller confirmation from a fresh-process read, and no test asserts
  that a rollback undoes a landed commit, that the stop fences direct gate or
  ledger callers or other receipts databases, that the marker proves store
  identity or freshness, or that the nonce authenticates anything.
"""

from __future__ import annotations

import ast
import hashlib
import multiprocessing
import os
import sqlite3
import threading
import time
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

import skippy.collaboration.offline_authenticated_adapter as adapter_module
from skippy.collaboration.authenticated_envelope import build_envelope
from skippy.collaboration.checkpoint_ledger import CheckpointLedger, CheckpointState
from skippy.collaboration.offline_admission_gate import OfflineAdmissionGate, ReservationResult
from skippy.collaboration.offline_authenticated_adapter import (
    SIGNED_MARKER_VALUE,
    OfflineAdapterError,
    OfflineAuthenticatedAdapter,
    SignedAdmissionError,
    provision_signed_receipts_store,
)
from skippy.collaboration.offline_signature_verifier import (
    KeyEntry,
    SignedEnvelope,
    _canonical_json,
    canonical_bytes,
    load_trust_root,
)

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
IMPL_SHA = "fedcba9876543210fedcba9876543210fedcba98"
PROJECT, TASK, CHECKPOINT, ASSIGNMENT = "QLA", "task-j6", "ckpt-j6", "assign-j6"
BARRIER_TIMEOUT = 10.0
START_TIMEOUT = 60.0
JOIN_TIMEOUT = 10.0
CONTENDED_BUSY_TIMEOUT = 20.0
SPAWN = multiprocessing.get_context("spawn")

KINDS = {
    "assignment": ("chief", "chief", "jason",
                   {"type": "assignment", "objective": "synthetic J6", "evidence_id": "fixture-assign",
                    "base_sha": BASE_SHA}),
    "implementation": ("jason", "implementer", "skippy",
                       {"type": "implementation", "evidence_id": "fixture-impl",
                        "implementation_sha": IMPL_SHA}),
    "review": ("skippy", "reviewer", "jason",
               {"type": "review", "evidence_id": "fixture-review", "implementation_sha": IMPL_SHA,
                "verdict": "approved"}),
    "correction": ("jason", "implementer", "skippy",
                   {"type": "correction", "evidence_id": "fixture-corr",
                    "implementation_sha": IMPL_SHA}),
}
SEQUENCE = {"assignment": 1, "implementation": 2, "review": 3, "correction": 2}


class InjectedFault(Exception):
    """Test-local injected failure (never raised by production code)."""


class Inconclusive(Exception):
    """A bounded schedule wait expired; never a passing outcome."""


# --- synthetic fixtures ------------------------------------------------------


def make_test_keys():
    actors = {"chief": ("chief", b"\x61" * 32), "jason": ("implementer", b"\x62" * 32),
              "skippy": ("reviewer", b"\x63" * 32)}
    signers, entries = {}, []
    for actor, (role, seed) in actors.items():
        key_id = "test-b2b2-j6-" + actor
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
    def read(self):
        return "clear"


class HaltCheck:
    """Injected adapter HALT dependency: 'clear', 'halted' or 'raise'."""

    def __init__(self, state="clear"):
        self.state = state
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.state == "raise":
            raise RuntimeError("synthetic HALT probe unavailable")
        return self.state == "halted"


def spec(kind="assignment", tag="p1", *, sequence=None, bad_signature=False, mid=None):
    """Plain, picklable description of one signed input."""
    return {"kind": kind, "mid": mid or f"j6-{kind}-{tag}", "idem": f"j6-idem-{kind}-{tag}",
            "sequence": SEQUENCE[kind] if sequence is None else sequence,
            "bad_signature": bad_signature}


def sign(item, signers) -> SignedEnvelope:
    sender, role, recipient, payload = KINDS[item["kind"]]
    key_id, signer = signers[sender]
    payload = dict(payload)
    unsigned = SignedEnvelope(
        schema_version=1, sender=sender, recipient=recipient, role=role, project_id=PROJECT,
        task_id=TASK, checkpoint_id=CHECKPOINT, assignment_id=ASSIGNMENT,
        message_id=item["mid"], idempotency_key=item["idem"], sequence=item["sequence"],
        timestamp=NOW - timedelta(seconds=60), expiry=NOW + timedelta(hours=1),
        payload_digest=hashlib.sha256(_canonical_json(payload)).digest(), payload=payload,
        key_id=key_id, signature=b"\x00" * 64,
    )
    signature = signer.sign(canonical_bytes(unsigned)).signature
    if item["bad_signature"]:
        signature = bytes([signature[0] ^ 0x01]) + signature[1:]
    return replace(unsigned, signature=signature)


class Env:
    """One fresh disposable store set: receipts, admission and ledger files."""

    def __init__(self, root, *, provision=True, journal_mode="delete", evidence="verified"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.receipts = self.root / "receipts.db"
        self.admission = self.root / "admission.db"
        self.ledger_path = self.root / "ledger.db"
        self.provision_config = (
            provision_signed_receipts_store(self.receipts, journal_mode=journal_mode)
            if provision else None
        )
        self.ledger = CheckpointLedger(self.ledger_path)
        self.trust_root, self.signers = make_test_keys()
        self.gate = OfflineAdmissionGate(self.admission, self.trust_root, FixtureEvidence(evidence),
                                         FixtureProbe())

    @classmethod
    def attach(cls, paths, *, evidence="verified"):
        env = cls.__new__(cls)
        env.root = Path(paths["root"])
        env.receipts = Path(paths["receipts"])
        env.admission = Path(paths["admission"])
        env.ledger_path = Path(paths["ledger"])
        env.provision_config = None
        env.ledger = CheckpointLedger(env.ledger_path)
        env.trust_root, env.signers = make_test_keys()
        env.gate = OfflineAdmissionGate(env.admission, env.trust_root, FixtureEvidence(evidence),
                                        FixtureProbe())
        return env

    @property
    def paths(self):
        return {"root": str(self.root), "receipts": str(self.receipts),
                "admission": str(self.admission), "ledger": str(self.ledger_path)}

    def adapter(self, *, halt="clear", trusted_fixture=True, busy_timeout=5.0):
        halt_check = HaltCheck(halt)
        adapter = OfflineAuthenticatedAdapter.open_signed(
            self.ledger, self.receipts, admission_gate=self.gate, halt_check=halt_check,
            trusted_fixture=trusted_fixture, busy_timeout=busy_timeout,
        )
        adapter.test_halt = halt_check
        return adapter

    def envelope(self, item):
        return sign(item, self.signers)


# --- durable-state observation (used in-process and in spawn children) -------

RECEIPT_TABLES = (
    ("fail_stop", "singleton"), ("intent_journal", "intent_id"), ("audit_receipts", "receipt_id"),
    ("sequence_state", "stream_key"), ("assignment_identity", "project_id, task_id"),
    ("signed_admission_marker", "singleton"),
)


def _ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)


def durable_state(paths) -> dict:
    """Read-only snapshot of every durable record the tests attribute writes to."""
    state = {}
    receipts = Path(paths["receipts"])
    state["receipts_exists"] = receipts.exists()
    if state["receipts_exists"]:
        try:
            con = _ro(receipts)
            try:
                for table, order in RECEIPT_TABLES:
                    try:
                        state[table] = con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
                    except sqlite3.Error as exc:
                        state[table] = f"unreadable: {type(exc).__name__}"
            finally:
                con.close()
        except sqlite3.Error as exc:
            state["receipts_error"] = type(exc).__name__
    admission = Path(paths["admission"])
    if admission.exists():
        con = _ro(admission)
        try:
            state["reservations"] = con.execute("SELECT * FROM reservations ORDER BY id").fetchall()
        finally:
            con.close()
    con = _ro(paths["ledger"])
    try:
        state["ledger_events"] = con.execute(
            "SELECT revision, event_type, project_id, task_id, checkpoint_id, message_id, "
            "idempotency_key, actor, payload FROM events ORDER BY revision"
        ).fetchall()
        state["ledger_revision"] = con.execute(
            "SELECT value FROM ledger_meta WHERE key = 'revision'"
        ).fetchall()
    finally:
        con.close()
    root = Path(paths["root"])
    state["files"] = sorted(
        name for name in os.listdir(root)
        if not name.startswith(("admission.db-", "ledger.db-"))
        and name not in ("receipts.db-wal", "receipts.db-shm")
    )
    return state


def raw_write(path, *statements):
    """Fixture-setup write through a separate raw connection (setup phase only)."""
    con = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        for sql, params in statements:
            con.execute(sql, params)
        con.execute("COMMIT")
    finally:
        con.close()


def stop_row(state):
    return state["fail_stop"]


def released(state):
    return state["fail_stop"] == [(1, 0, None)]


def admission_effects(state):
    """Every admission-effect record (J2 definition), for zero-effect comparisons."""
    accepted = [r for r in state["audit_receipts"] if r[8] == "accepted"]
    return (state["reservations"], state["intent_journal"], accepted, state["sequence_state"],
            state["assignment_identity"], state["ledger_events"], state["ledger_revision"])


# --- P-6 seam wrapper (test-local) -------------------------------------------

TARGETS = {
    "A": "UPDATE fail_stop SET stopped = 1",
    "E1.b": "INSERT INTO audit_receipts",
    "S10": "INSERT INTO audit_receipts",
    "W1": "INSERT INTO intent_journal",
    "S9": "INSERT INTO audit_receipts",
    "W3": "UPDATE intent_journal SET state = 'ACCEPTED'",
    "R": "UPDATE fail_stop SET stopped = 0",
}


def label_for(call_index, statements):
    joined = " | ".join(statements)
    if "UPDATE fail_stop SET stopped = 0" in joined:
        return "R"
    if "SET state = 'ACCEPTED'" in joined:
        return "W3"
    if "/*S9*/" in joined:
        return "S9"
    if "UPDATE fail_stop SET stopped = 1" in joined:
        return "A"
    if "INSERT INTO intent_journal" in joined:
        return "W1"
    if "INSERT INTO audit_receipts" in joined:
        return "E1.b" if call_index == 1 else "S10"
    return None


class Script:
    """Scripted fault, pause and observation plan for one adapter instance."""

    def __init__(self, *, fault=None, hooks=None, observe_after_begin=False, begin_requested=None):
        self.fault = fault                    # (label, condition) or None
        self.hooks = dict(hooks or {})        # point -> callable()
        self.observe_after_begin = observe_after_begin   # paths dict or False
        self.begin_requested = begin_requested
        self.calls = 0
        self.log = []                         # (call_index, event, detail)
        self.fired = False
        self.post_acquisition = []
        self.g_values = []

    def hook(self, point):
        callback = self.hooks.get(point)
        if callback is not None:
            callback()


class ScriptedConnection:
    def __init__(self, real, script, index):
        self._real = real
        self._script = script
        self._index = index
        self._statements = []

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def _label(self):
        return label_for(self._index, self._statements)

    def execute(self, sql, params=()):
        script = self._script
        flat = " ".join(sql.split())
        if flat.startswith("BEGIN"):
            self._statements = []
            if script.begin_requested is not None:
                script.begin_requested()
        if flat.startswith("UPDATE fail_stop SET stopped = 1"):
            script.hook("before_arm_update")
            script.g_values.append(params[0])
        cursor = self._real.execute(sql, params)
        script.log.append((self._index, "execute", flat))
        if flat.startswith("BEGIN") and script.observe_after_begin and not script.post_acquisition:
            row = self._real.execute("SELECT stopped, reason FROM fail_stop").fetchall()
            uncertain = self._real.execute(
                "SELECT COUNT(*) FROM audit_receipts WHERE outcome = 'uncertain'").fetchone()[0]
            script.post_acquisition.append({
                "fail_stop": row, "uncertain_receipts": uncertain,
                "state": durable_state(script.observe_after_begin),
            })
        if not flat.startswith(("BEGIN", "SELECT", "PRAGMA")):
            s9 = flat.startswith("INSERT INTO audit_receipts") and any(
                isinstance(p, str) and p.startswith("HALTED at reacquisition") for p in params)
            self._statements.append(flat + (" /*S9*/" if s9 else ""))
        fault = script.fault
        if (fault is not None and not script.fired and fault[1] in ("a", "RF")
                and flat.startswith(TARGETS[fault[0]]) and self._label() == fault[0]):
            script.fired = True
            script.log.append((self._index, "inject", f"{fault[0]}:{fault[1]}"))
            raise InjectedFault(f"injected pre-commit failure at {fault[0]}")
        return cursor

    def commit(self):
        script = self._script
        label = self._label()
        script.log.append((self._index, "commit", label))
        if label == "E1.b":
            script.hook("before_denial_commit")
        if label == "R":
            script.hook("R_before_commit")
        fault = script.fault
        if fault is not None and not script.fired and fault[0] == label and fault[1] in ("b", "c"):
            script.fired = True
            if fault[1] == "b":
                script.log.append((self._index, "inject", f"{label}:b"))
                raise InjectedFault(f"injected commit failure before delegating at {label}")
            self._real.commit()
            script.log.append((self._index, "inject", f"{label}:c"))
            raise InjectedFault(f"injected failure after real commit at {label}")
        self._real.commit()
        if label == "R":
            script.hook("after_R_commit")

    def rollback(self):
        script = self._script
        script.log.append((self._index, "rollback", self._label()))
        if script.fault is not None and script.fault[1] == "RF" and script.fired:
            raise InjectedFault("injected rollback failure (RF)")
        return self._real.rollback()

    def close(self):
        self._script.log.append((self._index, "close", None))
        return self._real.close()


def install_script(adapter, script):
    real_factory = adapter._receipts_connect

    def factory():
        script.calls += 1
        index = script.calls
        script.hook(f"before_connect_{index}")
        return ScriptedConnection(real_factory(), script, index)

    adapter._receipts_connect = factory
    return script


def spy_gate(env, mode=None, after=None):
    """Whole-call wrapper around gate.reserve; internal GATE:521 seam is BLOCKED."""
    real = env.gate.reserve
    counts = {"reserve": 0}

    def wrapped(snap, now):
        counts["reserve"] += 1
        if mode == "raise_without_delegating":
            raise InjectedFault("gate.reserve not invoked")
        if mode == "malformed":
            return ("reserved", "ok")
        if mode == "duplicate":
            return ReservationResult("duplicate", "identical reservation")
        result = real(snap, now)
        if after is not None:
            after(snap)
        if mode == "delegate_then_raise":
            raise InjectedFault("gate.reserve returned, then wrapper raised")
        return result

    env.gate.reserve = wrapped
    return counts


def spy_ledger(env, mode=None):
    """Whole-call wrapper around ledger.record_event; internal commit seam is BLOCKED."""
    real = env.ledger.record_event
    counts = {"record_event": 0}

    def wrapped(**kwargs):
        counts["record_event"] += 1
        if mode == "raise_without_delegating":
            raise InjectedFault("record_event not invoked")
        result = real(**kwargs)
        if mode == "delegate_then_raise":
            raise InjectedFault("record_event returned, then wrapper raised")
        return result

    env.ledger.record_event = wrapped
    return counts


def run(adapter, env, item, now=NOW):
    """Return ('accepted', None) or (classification, error)."""
    try:
        return adapter.accept_signed(env.envelope(item), now=now), None
    except SignedAdmissionError as exc:
        return exc.classification, exc


def statements(script):
    return [detail for _, event, detail in script.log if event == "execute"]


# --- spawn children (independent processes, fresh connections) ---------------

EVENT_NAMES = ("ready", "go", "begin_requested")


def _pause_hook(events, point):
    def pause():
        events[f"paused:{point}"].set()
        if not events[f"release:{point}"].wait(START_TIMEOUT):
            raise Inconclusive(f"release for {point} not received")
    return pause


def _child_main(job, conn, events):
    """Spawned child. Builds its own stores' objects and connections; never re-raises."""
    report = {"pid": os.getpid(), "error": None, "role": job["role"]}
    try:
        paths = job["paths"]
        if job["role"] == "observe":
            report["state"] = durable_state(paths)
        elif job["role"] == "legacy":
            events["ready"].set()
            if not events["go"].wait(START_TIMEOUT):
                raise Inconclusive("go not received")
            report["state_before"] = durable_state(paths)
            ledger = CheckpointLedger(Path(paths["ledger"]))
            try:
                legacy = OfflineAuthenticatedAdapter(ledger, Path(paths["receipts"]),
                                                     trusted_fixture=True, halt_check=lambda: False)
                envelope = build_envelope(
                    project_id=PROJECT, task_id=TASK, checkpoint_id=CHECKPOINT,
                    assignment_id=ASSIGNMENT, message_id="legacy-msg", idempotency_key="legacy-idem",
                    sender="chief", recipient="jason", role="chief",
                    payload={"type": "assignment", "objective": "legacy"}, timestamp=NOW,
                    expiry=NOW + timedelta(hours=1), sequence=1,
                )
                legacy.accept(envelope, now=NOW)
                report["result"] = "accepted"
            except OfflineAdapterError as exc:
                report["result"], report["message"] = "refused", str(exc)
            report["state_after"] = durable_state(paths)
        else:
            env = Env.attach(paths, evidence=job.get("evidence", "verified"))
            adapter = None
            try:
                adapter = env.adapter(halt=job.get("halt", "clear"),
                                      trusted_fixture=job.get("trusted", True),
                                      busy_timeout=job.get("busy_timeout", 5.0))
            except SignedAdmissionError as exc:
                report["construct_error"] = (exc.classification, str(exc))
            hooks = {point: _pause_hook(events, point) for point in job.get("pause_points", ())}
            script = Script(
                fault=tuple(job["fault"]) if job.get("fault") else None, hooks=hooks,
                observe_after_begin=paths if job.get("observe") else False,
                begin_requested=events["begin_requested"].set,
            )
            if adapter is not None:
                install_script(adapter, script)
            events["ready"].set()
            if job.get("wait_go", True) and not events["go"].wait(START_TIMEOUT):
                raise Inconclusive("go not received")
            report["state_before"] = durable_state(paths)
            if adapter is not None:
                result, error = run(adapter, env, job["input"])
                report["result"] = result
                report["message"] = None if error is None else str(error)
                report["step"] = None if error is None else error.step
            report["post_acquisition"] = script.post_acquisition
            report["g_values"] = script.g_values
            report["log"] = script.log
            report["state_after"] = durable_state(paths)
            if job.get("construct_after"):
                try:
                    env.adapter()
                    report["construct_after"] = ("ok", None)
                except SignedAdmissionError as exc:
                    report["construct_after"] = (exc.classification, str(exc))
    except BaseException:  # noqa: BLE001 - the parent receives the full traceback
        report["error"] = traceback.format_exc()
    try:
        conn.send(report)
    finally:
        conn.close()


class Child:
    def __init__(self, job):
        names = list(EVENT_NAMES)
        for point in job.get("pause_points", ()):
            names += [f"paused:{point}", f"release:{point}"]
        self.events = {name: SPAWN.Event() for name in names}
        self.receiver, sender = SPAWN.Pipe(duplex=False)
        self.process = SPAWN.Process(target=_child_main, args=(job, sender, self.events), daemon=True)
        self.process.start()
        sender.close()
        self._report = None
        self.killed = False

    def wait(self, name, timeout=BARRIER_TIMEOUT):
        if not self.events[name].wait(timeout):
            raise Inconclusive(f"child event {name!r} not observed within {timeout}s")

    def set(self, name):
        self.events[name].set()

    def report(self, timeout=START_TIMEOUT):
        if self._report is None:
            if not self.receiver.poll(timeout):
                raise Inconclusive(f"child result not received within {timeout}s")
            self._report = self.receiver.recv()
            assert self._report["error"] is None, self._report["error"]
        return self._report

    def kill(self):
        # After termination the child's shared Events must never be set again:
        # Condition.notify_all() would wait forever for a dead sleeper.
        self.killed = True
        self.process.terminate()
        self.process.join(JOIN_TIMEOUT)
        assert not self.process.is_alive()

    def stop(self):
        if not self.killed and self.process.is_alive():
            for name, event in self.events.items():
                if name.startswith(("release:", "go")) and not event.is_set():
                    event.set()
        if self.process.is_alive():
            self.process.join(2.0)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(JOIN_TIMEOUT)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(JOIN_TIMEOUT)
        self.receiver.close()


@pytest.fixture
def children():
    started = []

    def start(job):
        child = Child(job)
        started.append(child)
        return child

    try:
        yield start
    finally:
        for child in started:
            child.stop()
        assert not any(child.process.is_alive() for child in started), "child process survived"


def observe(start, env):
    """Fresh-process observation: a spawn child with new read-only connections."""
    return start({"role": "observe", "paths": env.paths}).report()["state"]


def contender(start, env, item, **job):
    """Independent signed participant, constructed now, run later on .set('go')."""
    child = start({"role": "contender", "paths": env.paths, "input": item, "observe": True,
                   "construct_after": True, **job})
    child.wait("ready", START_TIMEOUT)
    return child


def zero_writes(report):
    """The invocation under test changed nothing (rows or files) after its own setup."""
    baseline = (report["post_acquisition"][0]["state"] if report.get("post_acquisition")
                else report["state_before"])
    return report["state_after"] == baseline


# =============================================================================
# Provisioning (P-3), gated construction (P-4), P-6 configuration, legacy P-2
# =============================================================================


def _normalized_schema(path):
    con = _ro(path)
    try:
        rows = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    finally:
        con.close()
    return {(kind, name): " ".join((sql or "").split()) for kind, name, sql in rows}


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_provisioning_creates_existing_schema_plus_marker_and_reads_back_d1_d3(tmp_path, journal_mode):
    env = Env(tmp_path / "signed", journal_mode=journal_mode)
    legacy_path = tmp_path / "legacy-receipts.db"
    OfflineAuthenticatedAdapter(CheckpointLedger(tmp_path / "legacy-ledger.db"), legacy_path,
                                halt_check=lambda: False)
    signed_schema, legacy_schema = _normalized_schema(env.receipts), _normalized_schema(legacy_path)
    extra = set(signed_schema) - set(legacy_schema)
    assert extra == {("table", "signed_admission_marker")}
    assert set(legacy_schema) <= set(signed_schema)
    assert all(signed_schema[key] == legacy_schema[key] for key in legacy_schema)
    state = durable_state(env.paths)
    assert state["fail_stop"] == [(1, 0, None)]
    assert [row[:2] for row in state["signed_admission_marker"]] == [(1, SIGNED_MARKER_VALUE)]
    assert state["intent_journal"] == state["audit_receipts"] == state["sequence_state"] == []
    config = env.provision_config
    assert config["journal_mode"] == journal_mode
    assert config["synchronous"] in (2, 3) and config["locking_mode"] == "normal"
    adapter = env.adapter()
    observed = adapter._signed_last_config
    assert observed["journal_mode"] == journal_mode and observed["synchronous"] in (2, 3)
    assert observed["busy_timeout_ms"] == 5000 and observed["locking_mode"] == "normal"
    print(f"J6-OBSERVATION provision/open journal_mode={journal_mode} config={observed}")


def test_provisioning_refuses_every_existing_or_unsafe_target(tmp_path):
    env = Env(tmp_path / "existing")
    before = env.receipts.read_bytes()
    zero_byte = tmp_path / "zero.db"
    zero_byte.write_bytes(b"")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a sqlite store" * 64)
    legacy_path = tmp_path / "legacy.db"
    OfflineAuthenticatedAdapter(CheckpointLedger(tmp_path / "legacy-ledger.db"), legacy_path,
                                halt_check=lambda: False)
    legacy_bytes = legacy_path.read_bytes()
    for target in (env.receipts, zero_byte, junk, legacy_path):
        with pytest.raises(SignedAdmissionError, match="target exists") as info:
            provision_signed_receipts_store(target)
        assert info.value.classification == "refused"
    assert env.receipts.read_bytes() == before and legacy_path.read_bytes() == legacy_bytes
    assert zero_byte.read_bytes() == b"" and junk.read_bytes() == b"not a sqlite store" * 64
    for suffix in ("-journal", "-wal", "-shm"):
        orphan_main = tmp_path / f"orphan{suffix.replace('-', '_')}.db"
        Path(str(orphan_main) + suffix).write_bytes(b"orphan sidecar")
        with pytest.raises(SignedAdmissionError, match="orphan sidecar"):
            provision_signed_receipts_store(orphan_main)
        assert not orphan_main.exists()
    with pytest.raises(SignedAdmissionError, match="parent directory"):
        provision_signed_receipts_store(tmp_path / "missing-dir" / "r.db")
    for bad_mode in ("memory", "off", "WAL", "truncate", None):
        with pytest.raises(SignedAdmissionError, match="journal_mode"):
            provision_signed_receipts_store(tmp_path / "mode.db", journal_mode=bad_mode)
    for bad_path in (None, 123, b"bytes.db", "", ":memory:", "file:x.db?mode=rwc", True):
        with pytest.raises(SignedAdmissionError):
            provision_signed_receipts_store(bad_path)
    assert not (tmp_path / "mode.db").exists() and not (tmp_path / "missing-dir").exists()


def test_provisioning_refuses_existing_target_atomically_under_concurrency(tmp_path):
    target = tmp_path / "race.db"
    barrier = threading.Barrier(8)
    outcomes = []

    def provision():
        barrier.wait(BARRIER_TIMEOUT)
        try:
            provision_signed_receipts_store(target)
            outcomes.append("provisioned")
        except SignedAdmissionError as exc:
            # A loser may see the winner's transient journal ("orphan sidecar")
            # or the existing file ("target exists"); both are refusals.
            outcomes.append(("refused", exc.classification))

    threads = [threading.Thread(target=provision) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(BARRIER_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads)
    assert outcomes.count("provisioned") == 1
    assert outcomes.count(("refused", "refused")) == 7
    con = _ro(target)
    try:
        assert con.execute("SELECT * FROM fail_stop").fetchall() == [(1, 0, None)]
        assert [r[:2] for r in con.execute("SELECT * FROM signed_admission_marker")] == [
            (1, SIGNED_MARKER_VALUE)]
    finally:
        con.close()


def test_open_signed_refuses_invalid_dependencies_and_aliased_paths(tmp_path):
    env = Env(tmp_path)
    with pytest.raises(SignedAdmissionError, match="HALT"):
        OfflineAuthenticatedAdapter.open_signed(env.ledger, env.receipts, admission_gate=env.gate,
                                                halt_check=None)
    with pytest.raises(SignedAdmissionError, match="OfflineAdmissionGate"):
        OfflineAuthenticatedAdapter.open_signed(env.ledger, env.receipts, admission_gate=object(),
                                                halt_check=HaltCheck())
    with pytest.raises(SignedAdmissionError, match="pairwise distinct"):
        OfflineAuthenticatedAdapter.open_signed(env.ledger, env.ledger_path, admission_gate=env.gate,
                                                halt_check=HaltCheck())
    for timeout in (0, -1, 31, True, "5"):
        with pytest.raises(SignedAdmissionError, match="busy_timeout"):
            OfflineAuthenticatedAdapter.open_signed(env.ledger, env.receipts, admission_gate=env.gate,
                                                    halt_check=HaltCheck(), busy_timeout=timeout)
    adapter = env.adapter()
    assert adapter.execution_enabled is False and adapter.TRANSPORT_ENABLED is False
    adapter.TRANSPORT_ENABLED = True
    before = durable_state(env.paths)
    result, error = run(adapter, env, spec())
    assert (result, error.step) == ("refused", "S1") and durable_state(env.paths) == before


def test_signed_construction_never_creates_or_initializes_a_missing_store(tmp_path):
    env = Env(tmp_path, provision=False)
    before = sorted(os.listdir(tmp_path))
    with pytest.raises(SignedAdmissionError, match="receipts store unavailable") as info:
        env.adapter()
    assert info.value.classification == "refused"
    assert sorted(os.listdir(tmp_path)) == before and not env.receipts.exists()


def test_legacy_accept_refuses_marked_store_and_signed_instance_refuses_legacy_path(tmp_path):
    env = Env(tmp_path)
    legacy = OfflineAuthenticatedAdapter(env.ledger, env.receipts, trusted_fixture=True,
                                         halt_check=lambda: False)
    envelope = build_envelope(
        project_id=PROJECT, task_id=TASK, checkpoint_id=CHECKPOINT, assignment_id=ASSIGNMENT,
        message_id="legacy-1", idempotency_key="legacy-idem-1", sender="chief", recipient="jason",
        role="chief", payload={"type": "assignment", "objective": "legacy"}, timestamp=NOW,
        expiry=NOW + timedelta(hours=1), sequence=1,
    )
    before = durable_state(env.paths)
    with pytest.raises(OfflineAdapterError, match="signed_admission_marker"):
        legacy.accept(envelope, now=NOW)
    assert durable_state(env.paths) == before
    signed = env.adapter()
    with pytest.raises(OfflineAdapterError, match="legacy unsigned path disabled"):
        signed.accept(envelope, now=NOW)
    assert durable_state(env.paths) == before
    # Unmarked legacy store: existing behaviour unchanged.
    plain = OfflineAuthenticatedAdapter(env.ledger, tmp_path / "plain.db", trusted_fixture=True,
                                        halt_check=lambda: False)
    assert plain.accept(envelope, now=NOW) == "accepted"


# =============================================================================
# Positive flow and unfaulted controls
# =============================================================================


def test_signed_flow_accepts_assignment_implementation_review_and_releases_each_time(tmp_path, children):
    env = Env(tmp_path)
    adapter = env.adapter()
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    script = install_script(adapter, Script())
    for kind in ("assignment", "implementation", "review"):
        assert run(adapter, env, spec(kind)) == ("accepted", None)
        assert released(durable_state(env.paths))
    assert gate_counts == {"reserve": 3} and ledger_counts == {"record_event": 3}
    labels = [detail for _, event, detail in script.log if event == "commit"]
    assert labels == ["A", "W1", "W3", "R"] * 3
    state = observe(children, env)
    assert released(state)
    assert [row[9] for row in state["intent_journal"]] == ["ACCEPTED"] * 3
    assert [row[8] for row in state["audit_receipts"]] == ["accepted"] * 3
    assert state["sequence_state"] == [(f"{PROJECT}:{TASK}", 4)]
    assert state["assignment_identity"] == [(PROJECT, TASK, ASSIGNMENT)]
    assert [row[1] for row in state["ledger_events"]] == [
        "ASSIGNMENT_CREATED", "IMPLEMENTATION_SUBMITTED", "REVIEW_RECORDED"]
    import json
    payloads = [json.loads(row[8]) for row in state["ledger_events"]]
    assert payloads[0]["base_sha"] == BASE_SHA and "implementation_sha" not in payloads[0]
    assert payloads[1]["sha"] == payloads[1]["implementation_sha"] == IMPL_SHA
    assert all("source_verified" not in p and p["admission_path"] == "bridge-02b-signed" for p in payloads)
    assert len(payloads[0]) == 13 and len(payloads[1]) == len(payloads[2]) == 15
    assert env.ledger.status(PROJECT, TASK).state == CheckpointState.IMPLEMENTED_REPORTED
    # Fresh gated construction (reconcile plus signed-record validation) passes.
    env.adapter()


def test_definite_gate_rejection_records_receipt_then_releases_without_effects(tmp_path, children):
    env = Env(tmp_path, evidence="rejected")
    adapter = env.adapter()
    script = install_script(adapter, Script())
    result, error = run(adapter, env, spec())
    assert result == "rejected" and error.step == "S10" and not error.poisoned
    assert [d for _, e, d in script.log if e == "commit"] == ["A", "S10", "R"]
    state = observe(children, env)
    assert released(state)
    assert [(r[8], r[9]) for r in state["audit_receipts"]] == [("rejected", "admission: evidence rejected")]
    assert state["intent_journal"] == [] and state["ledger_events"] == [] and state["reservations"] == []


def test_s4_and_s7_refusals_write_nothing_and_never_arm(tmp_path):
    """S4 (signature) and S7 (ledger preflight) are not in the E1.b enumeration (C3)."""
    env = Env(tmp_path)
    adapter = env.adapter()
    counts = spy_gate(env)
    before = durable_state(env.paths)
    for item, needle in ((spec(bad_signature=True), "invalid signature"),
                         (spec("review", "early", sequence=1), "ledger preflight")):
        result, error = run(adapter, env, item)
        assert result == "refused" and needle in str(error) and not error.poisoned
        assert durable_state(env.paths) == before
    assert counts == {"reserve": 0}
    assert run(adapter, env, spec()) == ("accepted", None)


def test_reservation_read_back_mismatch_blocks_w1_and_keeps_g(tmp_path):
    env = Env(tmp_path)
    adapter = env.adapter()
    script = install_script(adapter, Script())

    def tamper(snap):
        raw_write(env.admission, ("UPDATE reservations SET role = 'tampered' WHERE message_id = ?",
                                  (snap.message_id,)))

    spy_gate(env, after=tamper)
    ledger_counts = spy_ledger(env)
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("refused", "S11", True)
    state = durable_state(env.paths)
    assert state["fail_stop"] == [(1, 1, script.g_values[0])]
    assert state["intent_journal"] == [] and ledger_counts == {"record_event": 0}


def test_signed_record_shape_violation_refuses_construction(tmp_path):
    env = Env(tmp_path)
    assert run(env.adapter(), env, spec()) == ("accepted", None)
    import json
    con = sqlite3.connect(env.ledger_path)
    try:
        payload = json.loads(con.execute("SELECT payload FROM events").fetchone()[0])
        payload.pop("evidence_kind")
        con.execute("UPDATE events SET payload = ?", (json.dumps(payload, sort_keys=True,
                                                                 separators=(",", ":")),))
        con.commit()
    finally:
        con.close()
    with pytest.raises(SignedAdmissionError, match="Derived signed record malformed"):
        env.adapter()


def test_source_check_unused_writers_and_reserve_callers():
    """J6 passing criterion 6: non-test callers of the excluded writers and of reserve."""
    root = Path(__file__).resolve().parents[1] / "skippy"
    callers = {"_advance_sequence": set(), "_record_receipt": set(), "reserve": set()}
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in callers:
                    callers[node.func.attr].add(source.name)
    assert callers["_advance_sequence"] == set() and callers["_record_receipt"] == set()
    # Adapter signed path (through the gate whole call) and the excluded SIM:287 direct caller.
    assert callers["reserve"] == {"offline_authenticated_adapter.py", "offline_bridge02b2_simulator.py"}


# =============================================================================
# M1 - arm-commit failure (conditions a, b, c, RF), retained-G and no-trace cases
# =============================================================================


@pytest.mark.parametrize("condition", ["a", "b", "c", "RF"])
def test_m1_arm_commit_fault_classification_and_independent_contender(tmp_path, children, condition):
    env = Env(tmp_path)
    adapter = env.adapter()
    other = contender(children, env, spec(tag="contender"))
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    script = install_script(adapter, Script(fault=("A", condition)))
    before = durable_state(env.paths)
    result, error = run(adapter, env, spec())
    g_value = script.g_values[0]
    expected = "confirmed_rolled_back" if condition == "a" else "ambiguous"
    assert (result, error.step, error.poisoned) == (expected, "A", True)
    if condition != "a":
        assert "rolled back" not in str(error) and "confirmed" not in str(error)
    assert gate_counts == {"reserve": 0} and ledger_counts == {"record_event": 0}
    assert script.calls == 1                                   # no T2, T3 or R connection
    assert [e for _, e, d in script.log if e == "inject"] == ["inject"]
    poisoned_result, poisoned_error = run(adapter, env, spec(tag="again"))
    assert poisoned_result == "refused" and "poisoned" in str(poisoned_error)
    assert script.calls == 1                                   # the poisoned instance never connects
    other.set("go")
    report = other.report()
    if condition == "c":
        assert report["state_before"]["fail_stop"] == [(1, 1, g_value)]
        assert report["post_acquisition"][0]["fail_stop"] == [(1, g_value)]
        assert report["result"] == "refused" and zero_writes(report)
        assert report["construct_after"][0] == "refused"
        assert durable_state(env.paths)["fail_stop"] == [(1, 1, g_value)]
    else:
        # (a), (b) and RF leave no durable trace (J3/E1.a residual): the fresh
        # process sees the pre-attempt state and is not stopped.
        assert report["state_before"] == before
        assert report["post_acquisition"][0]["fail_stop"] == [(0, None)]
        assert report["result"] == "accepted"


def test_m1_negative_control_unfaulted_arm_commits_and_flow_proceeds(tmp_path):
    env = Env(tmp_path)
    adapter = env.adapter()
    seen = {}
    script = install_script(adapter, Script(
        hooks={"before_connect_2": lambda: seen.update(durable_state(env.paths))}))
    assert run(adapter, env, spec()) == ("accepted", None)
    assert seen["fail_stop"] == [(1, 1, script.g_values[0])]
    assert script.g_values[0].startswith("SIGNED_ADMISSION_IN_PROGRESS v1 mid=")
    assert released(durable_state(env.paths))


# =============================================================================
# M2 - E1.b genuine-denial commit faults; Rule U positive and negative controls
# =============================================================================

DENIALS = {
    "halted": {"halt": "halted", "trusted_fixture": True, "outcome": "rejected", "reason": "HALTED"},
    "uncertain": {"halt": "raise", "trusted_fixture": True, "outcome": "uncertain",
                  "reason": "HALT check failed: RuntimeError"},
    "s6": {"halt": "clear", "trusted_fixture": False, "outcome": "rejected",
           "reason": "Assignment requires trusted fixture"},
}


@pytest.mark.parametrize("condition", ["a", "b", "c"])
@pytest.mark.parametrize("kind", sorted(DENIALS))
def test_m2_denial_commit_fault_never_arms_and_rule_u_controls(tmp_path, children, kind, condition):
    env = Env(tmp_path)
    denial = DENIALS[kind]
    adapter = env.adapter(halt=denial["halt"], trusted_fixture=denial["trusted_fixture"])
    later = env.adapter()                                       # constructed before the attempt
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    script = install_script(adapter, Script(fault=("E1.b", condition)))
    before = durable_state(env.paths)
    result, error = run(adapter, env, spec())
    expected = "confirmed_rolled_back" if condition == "a" else "ambiguous"
    assert (result, error.step, error.poisoned) == (expected, "E1.b", True)
    assert script.g_values == [] and gate_counts == {"reserve": 0} and ledger_counts == {"record_event": 0}
    assert script.calls == 1
    state = observe(children, env)
    assert released(state) and admission_effects(state) == admission_effects(before)
    receipts = [(row[1], row[8], row[9]) for row in state["audit_receipts"]]
    if condition == "c":
        assert receipts == [(spec()["mid"], denial["outcome"], denial["reason"])]
    else:
        assert receipts == []
    assert run(adapter, env, spec(tag="again"))[0] == "refused"   # no further call
    assert script.calls == 1
    pre = durable_state(env.paths)
    later_result, later_error = run(later, env, spec(tag="later"))
    if kind == "uncertain" and condition == "c":
        assert later_result == "refused" and "Rule U" in str(later_error)
        assert durable_state(env.paths) == pre
        with pytest.raises(SignedAdmissionError, match="Rule U"):
            env.adapter()
    else:
        assert later_result == "accepted"


@pytest.mark.parametrize("kind", sorted(DENIALS))
def test_m2_unfaulted_denial_commits_its_own_receipt_and_never_arms(tmp_path, children, kind):
    env = Env(tmp_path)
    denial = DENIALS[kind]
    adapter = env.adapter(halt=denial["halt"], trusted_fixture=denial["trusted_fixture"])
    later = env.adapter()
    gate_counts = spy_gate(env)
    script = install_script(adapter, Script())
    before = durable_state(env.paths)
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("rejected", "E1.b", False)
    assert script.g_values == [] and gate_counts == {"reserve": 0}
    assert [d for _, e, d in script.log if e == "commit"] == ["E1.b"]
    state = observe(children, env)
    assert released(state) and admission_effects(state) == admission_effects(before)
    assert [(r[1], r[8], r[9]) for r in state["audit_receipts"]] == [
        (spec()["mid"], denial["outcome"], denial["reason"])]
    # The same instance is not poisoned; with HALT now clear it may continue,
    # except where Rule U applies store-wide.
    adapter.test_halt.state = "clear"
    pre = durable_state(env.paths)
    if kind == "uncertain":
        for participant in (adapter, later):
            refused, refusal = run(participant, env, spec(tag="other-identity"))
            assert refused == "refused" and "Rule U" in str(refusal) and not refusal.poisoned
            assert durable_state(env.paths) == pre
        assert gate_counts == {"reserve": 0}
        with pytest.raises(SignedAdmissionError, match="Rule U"):
            env.adapter()
    else:
        assert run(later, env, spec(tag="other-identity")) == ("accepted", None)


def test_rule_u_absence_control_rejected_receipts_alone_do_not_block(tmp_path):
    env = Env(tmp_path)
    halted = env.adapter(halt="halted")
    untrusted = env.adapter(trusted_fixture=False)
    assert run(halted, env, spec(tag="h"))[0] == "rejected"
    assert run(untrusted, env, spec(tag="u"))[0] == "rejected"
    state = durable_state(env.paths)
    assert sorted(r[8] for r in state["audit_receipts"]) == ["rejected", "rejected"]
    assert run(env.adapter(), env, spec(tag="ok")) == ("accepted", None)


def test_rule_u_uncertain_intent_is_a_separate_reconcile_cause(tmp_path):
    env = Env(tmp_path)
    assert run(env.adapter(), env, spec()) == ("accepted", None)
    raw_write(env.receipts, ("UPDATE intent_journal SET state = 'UNCERTAIN'", ()))
    state = durable_state(env.paths)
    assert all(r[8] != "uncertain" for r in state["audit_receipts"])
    with pytest.raises(SignedAdmissionError, match="unresolved intent present"):
        env.adapter()


# =============================================================================
# M3 - post-arm effect failures (whole-call gate/ledger wrappers; P-6 receipts seams)
# =============================================================================

M3_CASES = [
    ("gate", "raise_without_delegating"), ("gate", "delegate_then_raise"),
    ("gate", "malformed"), ("gate", "duplicate"),
    ("W1", "a"), ("W1", "b"), ("W1", "c"),
    ("S9", "a"), ("S9", "b"), ("S9", "c"),
    ("S10", "a"), ("S10", "b"), ("S10", "c"),
    ("W2", "raise_without_delegating"), ("W2", "delegate_then_raise"),
    ("W3", "a"), ("W3", "b"), ("W3", "c"),
]
M3_STEP = {"S9": "S9", "gate": "S10", "W1": "W1", "S10": "S10", "W2": "W2", "W3": "W3"}


@pytest.mark.parametrize("seam,condition", M3_CASES, ids=[f"{s}-{c}" for s, c in M3_CASES])
def test_m3_post_arm_failure_retains_g_poisons_and_stops_contender(tmp_path, children, seam, condition):
    env = Env(tmp_path, evidence="rejected" if seam == "S10" else "verified")
    adapter = env.adapter()
    other = contender(children, env, spec(tag="contender"))
    gate_counts = spy_gate(env, mode=condition if seam == "gate" else None)
    ledger_counts = spy_ledger(env, mode=condition if seam == "W2" else None)
    receipts_fault = (seam, condition) if seam in ("W1", "S9", "S10", "W3") else None
    hooks = {}
    if seam == "S9":                       # HALT clear at S5, active at S9 (HALT #2)
        hooks["before_connect_2"] = lambda: setattr(adapter.test_halt, "state", "halted")
    script = install_script(adapter, Script(fault=receipts_fault, hooks=hooks))
    result, error = run(adapter, env, spec())
    g_value = script.g_values[0]
    expected = "confirmed_rolled_back" if condition == "a" else "ambiguous"
    assert (result, error.step, error.poisoned) == (expected, M3_STEP[seam], True)
    assert gate_counts == {"reserve": 0 if seam == "S9" else 1}  # no retry
    assert ledger_counts == {"record_event": 1 if seam in ("W2", "W3") else 0}
    assert not any(s.startswith("UPDATE fail_stop SET stopped = 0") for s in statements(script))
    calls = {"S9": 2, "gate": 2, "W1": 2, "S10": 2, "W2": 2, "W3": 3}[seam]
    assert script.calls == calls
    assert run(adapter, env, spec(tag="again"))[0] == "refused"
    assert script.calls == calls
    other.set("go")
    report = other.report()
    state = report["state_before"]
    assert state["fail_stop"] == [(1, 1, g_value)]                # G byte-equal, retained
    assert report["post_acquisition"][0]["fail_stop"] == [(1, g_value)]
    assert report["result"] == "refused" and zero_writes(report)
    assert report["construct_after"][0] == "refused"
    # No explanation or uncertain write after an ambiguous or failed effect.
    assert all(r[8] != "uncertain" for r in state["audit_receipts"])
    if seam == "W3" and condition == "c":
        assert [r[9] for r in state["intent_journal"]] == ["ACCEPTED"]
        assert [r[8] for r in state["audit_receipts"]] == ["accepted"]
        assert state["sequence_state"] == [(f"{PROJECT}:{TASK}", 2)]
    if seam == "S9":
        expected_receipts = [("rejected", "HALTED at reacquisition (S9); guard retained")]
        assert [(r[8], r[9]) for r in state["audit_receipts"]] == (
            expected_receipts if condition == "c" else [])
        assert state["reservations"] == [] and state["intent_journal"] == []
    if seam in ("W2", "W3") and condition != "c":
        assert [r[9] for r in state["intent_journal"]] == ["PENDING"]   # not marked UNCERTAIN


def test_s9_halt_at_reacquisition_keeps_g_byte_equal_and_records_one_receipt(tmp_path, children):
    env = Env(tmp_path)
    adapter = env.adapter()
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    script = install_script(adapter, Script(
        hooks={"before_connect_2": lambda: setattr(adapter.test_halt, "state", "halted")}))
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("stop_retained", "S9", True)
    assert gate_counts == {"reserve": 0} and ledger_counts == {"record_event": 0}
    assert [d for _, e, d in script.log if e == "commit"] == ["A", "S9"]
    state = observe(children, env)
    assert state["fail_stop"] == [(1, 1, script.g_values[0])]      # C2: S9 never writes fail_stop
    assert [(r[8], r[9]) for r in state["audit_receipts"]] == [
        ("rejected", "HALTED at reacquisition (S9); guard retained")]
    assert state["reservations"] == [] and state["intent_journal"] == []


def test_m3_m4_negative_control_confirmed_release_lets_contender_proceed(tmp_path, children):
    env = Env(tmp_path)
    adapter = env.adapter()
    other = contender(children, env, spec("implementation", "contender"))
    script = install_script(adapter, Script())
    assert run(adapter, env, spec()) == ("accepted", None)
    assert [d for _, e, d in script.log if e == "commit"] == ["A", "W1", "W3", "R"]
    other.set("go")
    report = other.report()
    assert report["post_acquisition"][0]["fail_stop"] == [(0, None)]
    assert report["result"] == "accepted"


# =============================================================================
# M4 - release-commit failure
# =============================================================================


@pytest.mark.parametrize("condition", ["a", "b", "c"])
def test_m4_release_fault_poisons_without_retry(tmp_path, children, condition):
    env = Env(tmp_path)
    adapter = env.adapter()
    other = contender(children, env, spec("implementation", "contender"))
    pre_release = {}
    script = install_script(adapter, Script(
        fault=("R", condition),
        hooks={"before_connect_4": lambda: pre_release.update(durable_state(env.paths))}))
    result, error = run(adapter, env, spec())
    g_value = script.g_values[0]
    expected = "confirmed_rolled_back" if condition == "a" else "ambiguous"
    assert (result, error.step, error.poisoned) == (expected, "R", True)
    assert sum(s.startswith("UPDATE fail_stop SET stopped = 0") for s in statements(script)) == 1
    assert run(adapter, env, spec("implementation", "again"))[0] == "refused"
    assert script.calls == 4
    other.set("go")
    report = other.report()
    state = report["state_before"]
    if condition in ("a", "b"):
        assert state["fail_stop"] == [(1, 1, g_value)]
        assert report["result"] == "refused" and zero_writes(report)
    else:
        # (c) release-only residual: the only difference from the pre-R state
        # is the cleared stop; reconcile passes in a fresh process.
        assert pre_release["fail_stop"] == [(1, 1, g_value)] and released(state)
        assert {k: v for k, v in state.items() if k != "fail_stop"} == {
            k: v for k, v in pre_release.items() if k != "fail_stop"}
        assert "construct_error" not in report
        assert report["result"] == "accepted"                 # E2 residual recorded, not a global stop


# =============================================================================
# M5 - stale or foreign G at T2, T3 and R
# =============================================================================


@pytest.mark.parametrize("substitute", ["foreign", "stale"])
@pytest.mark.parametrize("step,index", [("T2", 2), ("T3", 3), ("R", 4)])
def test_m5_owner_predicate_refuses_substituted_reason(tmp_path, step, index, substitute):
    env = Env(tmp_path)
    adapter = env.adapter()
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    holder = {}

    def substitute_reason():
        g_value = script.g_values[0]
        value = ("FOREIGN manual stop (synthetic)" if substitute == "foreign"
                 else g_value[: -32] + "0" * 32)
        assert value != g_value
        raw_write(env.receipts, ("UPDATE fail_stop SET reason = ? WHERE singleton = 1", (value,)))
        holder["value"], holder["state"] = value, durable_state(env.paths)

    script = install_script(adapter, Script(hooks={f"before_connect_{index}": substitute_reason}))
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("refused", step, True)
    after = durable_state(env.paths)
    assert after == holder["state"]                            # the owner step wrote nothing
    assert after["fail_stop"] == [(1, 1, holder["value"])]     # foreign stop untouched, no release
    assert gate_counts == {"reserve": 0 if step == "T2" else 1}
    assert ledger_counts == {"record_event": 0 if step == "T2" else 1}
    assert script.calls == index
    assert run(adapter, env, spec(tag="again"))[0] == "refused" and script.calls == index


def test_m5_negative_control_matching_g_at_each_owner_step(tmp_path):
    env = Env(tmp_path)
    adapter = env.adapter()
    seen = []
    script = install_script(adapter, Script(hooks={
        f"before_connect_{i}": (lambda: seen.append(durable_state(env.paths)["fail_stop"]))
        for i in (2, 3, 4)}))
    assert run(adapter, env, spec()) == ("accepted", None)
    assert seen == [[(1, 1, script.g_values[0])]] * 3
    assert released(durable_state(env.paths))


# =============================================================================
# M6 - child terminated at the seams
# =============================================================================

M6_PAUSE = {"before_A": "before_arm_update", "after_A": "before_connect_2",
            "after_final_effect": "before_connect_4", "after_R": "after_R_commit"}


@pytest.mark.parametrize("point", list(M6_PAUSE))
def test_m6_child_terminated_at_seam(tmp_path, children, point):
    env = Env(tmp_path)
    resubmit = point == "after_R"
    other = contender(children, env, spec(tag="p1") if resubmit else spec(tag="contender"))
    baseline = durable_state(env.paths)
    pause = M6_PAUSE[point]
    p1 = children({"role": "signed", "paths": env.paths, "input": spec(tag="p1"),
                   "pause_points": [pause], "wait_go": False})
    p1.wait("ready", START_TIMEOUT)
    p1.wait(f"paused:{pause}")
    p1.kill()
    other.set("go")
    report = other.report()
    state = report["state_before"]
    if point == "before_A":
        assert state == baseline                                 # no trace (E1)
        assert report["post_acquisition"][0]["fail_stop"] == [(0, None)]
        assert report["result"] == "accepted"
        return
    if point in ("after_A", "after_final_effect"):
        (singleton, stopped, reason), = state["fail_stop"]
        assert stopped == 1 and reason.startswith("SIGNED_ADMISSION_IN_PROGRESS v1 mid=")
        assert report["post_acquisition"][0]["fail_stop"] == [(1, reason)]
        assert report["result"] == "refused" and zero_writes(report)
        assert report["construct_after"][0] == "refused"
        if point == "after_final_effect":
            assert [r[9] for r in state["intent_journal"]] == ["ACCEPTED"]
            assert [r[8] for r in state["audit_receipts"]] == ["accepted"]
        else:
            assert state["intent_journal"] == [] and state["reservations"] == []
        return
    # after R: released and admission durable; the same identity is refused before arming.
    assert released(state)
    assert [r[9] for r in state["intent_journal"]] == ["ACCEPTED"]
    assert [r[8] for r in state["audit_receipts"]] == ["accepted"]
    assert len(state["ledger_events"]) == 1
    assert report["result"] in ("rejected", "refused") and report["g_values"] == []
    assert released(report["state_after"])
    assert admission_effects(report["state_after"]) == admission_effects(state)


# =============================================================================
# M7a / M7b - two independent processes around G
# =============================================================================


def test_m7a_p2_acquires_while_g_remains_and_is_refused_with_zero_writes(tmp_path, children):
    env = Env(tmp_path)
    p2_inputs = {
        "authorized": ({}, spec(tag="p2")),
        "s6_reject": ({"trusted": False}, spec(tag="p2s6")),
        "halt_active": ({"halt": "halted"}, spec(tag="p2halt")),
        "bad_signature": ({}, spec(tag="p2sig", bad_signature=True)),
    }
    contenders = {name: contender(children, env, item, **job) for name, (job, item) in p2_inputs.items()}
    p1 = children({"role": "signed", "paths": env.paths, "input": spec(tag="p1"),
                   "pause_points": ["before_connect_2"], "wait_go": False})
    p1.wait("ready", START_TIMEOUT)
    p1.wait("paused:before_connect_2")                         # confirmed A, no lock held
    armed = durable_state(env.paths)
    g_p1 = armed["fail_stop"][0][2]
    assert armed["fail_stop"] == [(1, 1, g_p1)]
    for name, child in contenders.items():
        child.set("go")
        report = child.report()
        assert report["post_acquisition"][0]["fail_stop"] == [(1, g_p1)], name
        assert report["result"] == "refused" and report["step"] == "T1", name
        assert zero_writes(report) and report["g_values"] == [], name
    legacy = children({"role": "legacy", "paths": env.paths})
    legacy.wait("ready", START_TIMEOUT)
    legacy.set("go")
    legacy_report = legacy.report()
    assert legacy_report["result"] == "refused" and "Fail-stop active" in legacy_report["message"]
    assert legacy_report["state_after"] == legacy_report["state_before"]
    assert durable_state(env.paths) == armed
    p1.set("release:before_connect_2")
    p1_report = p1.report()
    assert p1_report["result"] == "accepted" and p1_report["g_values"] == [g_p1]


def test_m7a_negative_control_signed_p2_alone_on_released_store_proceeds(tmp_path, children):
    env = Env(tmp_path)
    p2 = contender(children, env, spec(tag="p2"))
    p2.set("go")
    report = p2.report()
    assert report["post_acquisition"][0]["fail_stop"] == [(0, None)]
    assert report["result"] == "accepted"


def test_m7b_p2_blocked_through_confirmed_release_then_observes_released_state(tmp_path, children):
    env = Env(tmp_path)
    p2 = contender(children, env, spec("implementation", "p2"), busy_timeout=CONTENDED_BUSY_TIMEOUT)
    p1 = children({"role": "signed", "paths": env.paths, "input": spec(tag="p1"),
                   "pause_points": ["R_before_commit"], "wait_go": False})
    p1.wait("ready", START_TIMEOUT)
    p1.wait("paused:R_before_commit")                          # conditional update done, lock held
    p2.set("go")
    p2.wait("begin_requested")
    time.sleep(0.5)
    assert not p2.receiver.poll(0), "P2 completed while P1 held the receipts lock"
    p1.set("release:R_before_commit")
    p1_report = p1.report()
    assert p1_report["result"] == "accepted"
    report = p2.report()
    if report["result"] == "refused" and "lock not acquired" in (report["message"] or ""):
        raise Inconclusive("P2 busy wait expired: inconclusive, not a pass")
    assert report["post_acquisition"][0]["fail_stop"] == [(0, None)]
    assert report["result"] == "accepted"                      # may proceed; other checks pass
    legacy = children({"role": "legacy", "paths": env.paths})
    legacy.wait("ready", START_TIMEOUT)
    legacy.set("go")
    legacy_report = legacy.report()
    assert legacy_report["result"] == "refused"
    assert "signed_admission_marker" in legacy_report["message"]
    assert legacy_report["state_after"] == legacy_report["state_before"]


# =============================================================================
# M8 - storage and provisioning faults
# =============================================================================


class _PragmaLie:
    """Fault fixture: a real connection whose D1 or D2 read-back is falsified."""

    def __init__(self, real, lie):
        self._real, self._lie = real, lie

    def execute(self, sql, params=()):
        flat = " ".join(sql.split()).upper()
        if flat == self._lie[0]:
            return self._real.execute("SELECT ?", (self._lie[1],))
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_pragma_lie(monkeypatch, lie):
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        return _PragmaLie(real_connect(*args, **kwargs), lie)

    shim = type(sqlite3)("sqlite3_shim")
    shim.__dict__.update({k: v for k, v in vars(sqlite3).items() if not k.startswith("__")})
    shim.connect = connect
    monkeypatch.setattr(adapter_module, "sqlite3", shim)


M8_CASES = ["healthy", "missing_file", "missing_marker_row", "marker_table_dropped", "non_sqlite_bytes",
            "fail_stop_row_deleted", "fail_stop_row_duplicated", "locked_past_timeout",
            "d1_journal_mode_mismatch", "d2_synchronous_mismatch"]


@pytest.mark.parametrize("case", M8_CASES)
def test_m8_storage_fault_refuses_with_no_gate_call_and_zero_writes(tmp_path, monkeypatch, case):
    env = Env(tmp_path)
    adapter = env.adapter(busy_timeout=0.5)
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    holder = None
    # --- setup phase (fault fixture), recorded separately ---
    if case == "missing_file":
        env.receipts.unlink()
    elif case == "missing_marker_row":
        raw_write(env.receipts, ("DELETE FROM signed_admission_marker", ()))
    elif case == "marker_table_dropped":
        raw_write(env.receipts, ("DROP TABLE signed_admission_marker", ()))
    elif case == "non_sqlite_bytes":
        env.receipts.write_bytes(b"NOT-SQLITE " * 512)
    elif case == "fail_stop_row_deleted":
        raw_write(env.receipts, ("DELETE FROM fail_stop", ()))
    elif case == "fail_stop_row_duplicated":
        # The schema's CHECK (singleton = 1) prevents a second row; the fixture
        # therefore replaces the table, which phase 1 must refuse.
        with pytest.raises(sqlite3.IntegrityError):
            raw_write(env.receipts, ("INSERT INTO fail_stop VALUES (2, 0, NULL)", ()))
        raw_write(env.receipts, ("DROP TABLE fail_stop", ()),
                  ("CREATE TABLE fail_stop (singleton INTEGER, stopped INTEGER NOT NULL, reason TEXT)", ()),
                  ("INSERT INTO fail_stop VALUES (1, 0, NULL), (1, 0, NULL)", ()))
    elif case == "locked_past_timeout":
        holder = sqlite3.connect(env.receipts, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
    elif case == "d1_journal_mode_mismatch":
        _install_pragma_lie(monkeypatch, ("PRAGMA JOURNAL_MODE", "memory"))
    elif case == "d2_synchronous_mismatch":
        _install_pragma_lie(monkeypatch, ("PRAGMA SYNCHRONOUS", 1))
    raw_before = env.receipts.read_bytes() if env.receipts.exists() else None
    before = durable_state(env.paths) if holder is None else None
    try:
        result, error = run(adapter, env, spec())
    finally:
        if holder is not None:
            holder.rollback()
            holder.close()
    if before is None:
        before = durable_state(env.paths)
    if case == "healthy":
        assert result == "accepted"
        return
    assert result == "refused" and error.step == "T1", (result, error)
    assert gate_counts == {"reserve": 0} and ledger_counts == {"record_event": 0}
    assert durable_state(env.paths) == before
    after_bytes = env.receipts.read_bytes() if env.receipts.exists() else None
    if case != "locked_past_timeout":
        assert after_bytes == raw_before
    assert env.receipts.exists() == (case != "missing_file")   # nothing recreated
    if case == "locked_past_timeout":
        assert "database is locked" in str(error) or "lock not acquired" in str(error)
    if case == "locked_past_timeout":
        env.adapter()                          # lock released: construction proceeds
    else:
        with pytest.raises(SignedAdmissionError):
            env.adapter()
    if case not in ("missing_file", "locked_past_timeout"):
        assert (env.receipts.read_bytes() if env.receipts.exists() else None) == raw_before
    if case.startswith("d"):
        monkeypatch.undo()
        assert run(env.adapter(), env, spec(tag="control")) == ("accepted", None)


def test_m8_store_deleted_after_arming_refuses_at_t2_and_recreates_nothing(tmp_path):
    env = Env(tmp_path)
    adapter = env.adapter()
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    seen = {}

    def delete_store():
        seen["armed"] = durable_state(env.paths)
        env.receipts.unlink()
        seen["setup"] = durable_state(env.paths)

    script = install_script(adapter, Script(hooks={"before_connect_2": delete_store}))
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("refused", "T2", True)
    assert seen["armed"]["fail_stop"] == [(1, 1, script.g_values[0])]   # the earlier confirmed arm
    assert gate_counts == {"reserve": 0} and ledger_counts == {"record_event": 0}
    assert durable_state(env.paths) == seen["setup"] and not env.receipts.exists()
    assert run(adapter, env, spec(tag="again"))[0] == "refused" and not env.receipts.exists()


# =============================================================================
# M9 - restart and fresh-process construction
# =============================================================================


def _construction_spy(monkeypatch):
    executed = []
    real = OfflineAuthenticatedAdapter._receipts_connect

    def spy(self):
        script = Script()
        connection = ScriptedConnection(real(self), script, 1)
        executed.append(script)
        return connection

    monkeypatch.setattr(OfflineAuthenticatedAdapter, "_receipts_connect", spy)
    return lambda: [stmt for s in executed for _, event, stmt in s.log if event == "execute"]


@pytest.mark.parametrize("condition", ["g_present", "pending_present", "uncertain_receipt_only", "released"])
def test_m9_restart_construction_never_parses_reason_and_never_releases(tmp_path, monkeypatch, children,
                                                                        condition):
    env = Env(tmp_path)
    if condition == "g_present":
        spy_gate(env, mode="raise_without_delegating")
        assert run(env.adapter(), env, spec())[0] == "ambiguous"
    elif condition == "pending_present":
        assert run(env.adapter(), env, spec()) == ("accepted", None)
        raw_write(env.receipts, ("UPDATE intent_journal SET state = 'PENDING'", ()))
    elif condition == "uncertain_receipt_only":
        assert run(env.adapter(halt="raise"), env, spec())[0] == "rejected"
    else:
        assert run(env.adapter(), env, spec()) == ("accepted", None)
    before = durable_state(env.paths)
    executed = _construction_spy(monkeypatch)
    if condition == "released":
        env.adapter()
    else:
        with pytest.raises(SignedAdmissionError) as info:
            env.adapter()
        assert info.value.classification == "refused"
    statements_run = executed()
    assert statements_run and all(
        "reason" not in stmt and "*" not in stmt for stmt in statements_run if "fail_stop" in stmt)
    assert not any(stmt.startswith(("UPDATE", "INSERT", "DELETE")) for stmt in statements_run)
    assert durable_state(env.paths) == before                  # no release on restart
    state = observe(children, env)
    assert state == before


# =============================================================================
# M10 - T1 boundary, two processes
# =============================================================================

P2_INPUTS = {
    "bad_signature": ({}, {"bad_signature": True}),
    "halt_active": ({"halt": "halted"}, {}),
    "s6_reject": ({"trusted": False}, {}),
    "authorized": ({}, {}),
}
M10_RUNS = [("i", None), ("ii", None), ("iii", "halted"), ("iii", "s6"), ("iii", "uncertain")]


def _m10_cases():
    for run_kind, denial in M10_RUNS:
        for p2 in P2_INPUTS:
            yield pytest.param(run_kind, denial, p2, id=f"{run_kind}-{denial or 'arm'}-{p2}")


@pytest.mark.parametrize("run_kind,denial,p2_input", list(_m10_cases()))
def test_m10_t1_boundary_two_processes(tmp_path, children, run_kind, denial, p2_input):
    env = Env(tmp_path)
    p2_job, p2_spec = P2_INPUTS[p2_input]
    p2 = contender(children, env, spec(tag="p2", **p2_spec), busy_timeout=CONTENDED_BUSY_TIMEOUT, **p2_job)
    baseline = durable_state(env.paths)
    p1_job = {"role": "signed", "paths": env.paths, "input": spec(tag="p1"), "wait_go": False}
    if run_kind == "i":
        p1_job["pause_points"] = ["before_arm_update", "before_connect_2"]
    elif run_kind == "ii":
        p1_job.update(pause_points=["before_arm_update"], fault=("A", "a"))
    else:
        p1_job["pause_points"] = ["before_denial_commit"]
        p1_job.update({"halted": {"halt": "halted"}, "s6": {"trusted": False},
                       "uncertain": {"halt": "raise"}}[denial])
    first = p1_job["pause_points"][0]
    p1 = children(p1_job)
    p1.wait("ready", START_TIMEOUT)
    p1.wait(f"paused:{first}")                                 # inside T1, T1 lock held
    p2.set("go")
    p2.wait("begin_requested")
    time.sleep(0.3)
    assert not p2.receiver.poll(0), "P2 completed while P1 held the T1 lock"
    p1.set(f"release:{first}")
    if run_kind == "i":
        p1.wait("paused:before_connect_2")                     # A confirmed; no lock held
    report = p2.report()
    if report["result"] == "refused" and "lock not acquired" in (report["message"] or ""):
        raise Inconclusive("P2 busy wait expired: inconclusive, not a pass")
    if run_kind == "i":
        p1.set("release:before_connect_2")
    p1_report = p1.report()
    seen = report["post_acquisition"][0]
    p2_mid = spec(tag="p2")["mid"]
    p2_receipts = [r for r in report["state_after"]["audit_receipts"] if r[1] == p2_mid]
    if run_kind == "i":
        g_p1 = p1_report["g_values"][0]
        assert seen["fail_stop"] == [(1, g_p1)]
        assert report["result"] == "refused" and zero_writes(report) and p2_receipts == []
        assert p1_report["result"] == "accepted"
        return
    if run_kind == "ii":
        assert p1_report["result"] == "confirmed_rolled_back" and p1_report["g_values"]
        assert seen["fail_stop"] == [(0, None)] and seen["state"] == baseline   # no P1 trace
    else:
        assert p1_report["result"] == "rejected" and p1_report["g_values"] == []   # P1 never arms
        outcome = "uncertain" if denial == "uncertain" else "rejected"
        p1_rows = [r for r in seen["state"]["audit_receipts"] if r[1] == spec(tag="p1")["mid"]]
        assert [r[8] for r in p1_rows] == [outcome]
        assert seen["uncertain_receipts"] == (1 if denial == "uncertain" else 0)
        assert {k: v for k, v in seen["state"].items() if k != "audit_receipts"} == {
            k: v for k, v in baseline.items() if k != "audit_receipts"}
        assert seen["fail_stop"] == [(0, None)]
        if denial == "uncertain":
            assert report["result"] == "refused" and "Rule U" in report["message"]
            assert zero_writes(report) and p2_receipts == [] and report["g_values"] == []
            return
    # P2 runs its own checks (ii, and the rejected sub-cases of iii).
    if p2_input == "authorized":
        assert report["result"] == "accepted" and len(report["g_values"]) == 1
    elif p2_input == "bad_signature":
        # C3/S4: verification failure is refused with no receipt (see the
        # documented conflict with the M10 negative-control wording).
        assert report["result"] == "refused" and "invalid signature" in report["message"]
        assert zero_writes(report) and p2_receipts == [] and report["g_values"] == []
    else:
        assert report["result"] == "rejected" and report["step"] == "E1.b"
        assert [r[8] for r in p2_receipts] == ["rejected"] and report["g_values"] == []


@pytest.mark.parametrize("p2_input", ["bad_signature", "s6_reject", "halt_active"])
def test_m10_negative_control_p2_alone(tmp_path, children, p2_input):
    """P2 alone on a fresh store. J5's negative control expects a bad-signature
    receipt; C3/E1.b and 6838692613 S4 enumerate no receipt before verification.
    The implementation follows C3 (no receipt, no arm); the conflict is reported."""
    env = Env(tmp_path)
    p2_job, p2_spec = P2_INPUTS[p2_input]
    p2 = contender(children, env, spec(tag="p2", **p2_spec), **p2_job)
    p2.set("go")
    report = p2.report()
    assert report["g_values"] == [] and released(report["state_after"])
    receipts = report["state_after"]["audit_receipts"]
    if p2_input == "bad_signature":
        assert report["result"] == "refused" and receipts == [] and zero_writes(report)
    else:
        assert report["result"] == "rejected" and [r[8] for r in receipts] == ["rejected"]
