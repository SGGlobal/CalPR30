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
        if self.verdict == "raise":
            raise RuntimeError("synthetic evidence dependency failure")
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


def spec(kind="assignment", tag="p1", *, sequence=None, bad_signature=False, mid=None,
         recipient=None, payload_set=None, payload_drop=()):
    """Plain, picklable description of one signed input (validly signed unless bad_signature)."""
    return {"kind": kind, "mid": mid or f"j6-{kind}-{tag}", "idem": f"j6-idem-{kind}-{tag}",
            "sequence": SEQUENCE[kind] if sequence is None else sequence,
            "bad_signature": bad_signature, "recipient": recipient,
            "payload_set": dict(payload_set or {}), "payload_drop": tuple(payload_drop)}


def sign(item, signers) -> SignedEnvelope:
    sender, role, recipient, payload = KINDS[item["kind"]]
    key_id, signer = signers[sender]
    payload = dict(payload)
    if item.get("recipient") is not None:
        recipient = item["recipient"]
    payload.update(item.get("payload_set") or {})
    for key in item.get("payload_drop") or ():
        payload.pop(key, None)
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


def spy_evidence(env, log=None):
    """Records every call to the configured gate.evidence_verifier (pre-arm and gate)."""
    verifier = env.gate.evidence_verifier
    real = verifier.verify
    calls = []

    def wrapped(*args):
        calls.append(args)
        if log is not None:
            log.append((0, "evidence", args[:4]))
        return real(*args)

    verifier.verify = wrapped
    return calls


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
    envelope = build_envelope(
        project_id=PROJECT, task_id=TASK, checkpoint_id=CHECKPOINT, assignment_id=ASSIGNMENT,
        message_id="legacy-1", idempotency_key="legacy-idem-1", sender="chief", recipient="jason",
        role="chief", payload={"type": "assignment", "objective": "legacy"}, timestamp=NOW,
        expiry=NOW + timedelta(hours=1), sequence=1,
    )
    # P-9 (work order 6870819489, amendment 6871180075): the legacy constructor now
    # refuses a marked store before any write. This intentionally supersedes the
    # former accept-stage expectation (construction no longer succeeds here).
    before = durable_state(env.paths)
    with pytest.raises(OfflineAdapterError, match="signed_admission_marker"):
        OfflineAuthenticatedAdapter(env.ledger, env.receipts, trusted_fixture=True,
                                    halt_check=lambda: False)
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


def test_post_arm_evidence_change_is_rejected_by_authoritative_gate_under_g(tmp_path, children):
    """Ruling C: pre-arm evidence passes, then the dependency changes after a
    confirmed A; the gate re-verifies inside reserve and definitively rejects
    (S10 receipt, then the separate exact-G release R). Distinct from a
    pre-arm denial, which never arms."""
    env = Env(tmp_path)
    adapter = env.adapter()
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    script = Script(hooks={
        "before_connect_2": lambda: setattr(env.gate.evidence_verifier, "verdict", "rejected")})
    install_script(adapter, script)
    evidence = spy_evidence(env, script.log)
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("rejected", "S10", False)
    expected_call = ("chief_authorization", ASSIGNMENT, BASE_SHA, "fixture-assign", NOW)
    assert evidence == [expected_call, expected_call]          # pre-arm, then the gate
    order = [(e, d) for _, e, d in script.log if e in ("evidence", "commit")]
    assert order == [("evidence", expected_call[:4]), ("commit", "A"),
                     ("evidence", expected_call[:4]), ("commit", "S10"), ("commit", "R")]
    assert gate_counts == {"reserve": 1} and ledger_counts == {"record_event": 0}
    state = observe(children, env)
    assert released(state)
    assert [(r[8], r[9]) for r in state["audit_receipts"]] == [("rejected", "admission: evidence rejected")]
    assert state["intent_journal"] == [] and state["ledger_events"] == [] and state["reservations"] == []
    assert state["sequence_state"] == [] and state["assignment_identity"] == []


def test_s4_and_s7_refusals_write_nothing_and_never_arm(tmp_path):
    """S4 (signature) and S7 (ledger preflight) are not in the E1.b enumeration (C3)."""
    env = Env(tmp_path)
    adapter = env.adapter()
    counts = spy_gate(env)
    script = install_script(adapter, Script())
    evidence = spy_evidence(env)
    before = durable_state(env.paths)
    for item, needle in ((spec(bad_signature=True), "invalid signature"),
                         (spec("review", "early", sequence=1), "ledger preflight")):
        result, error = run(adapter, env, item)
        assert result == "refused" and needle in str(error) and not error.poisoned
        assert durable_state(env.paths) == before
    assert counts == {"reserve": 0} and evidence == [] and script.g_values == []
    assert run(adapter, env, spec()) == ("accepted", None)
    assert len(evidence) == 2


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
    env = Env(tmp_path)
    adapter = env.adapter()
    other = contender(children, env, spec(tag="contender"))
    gate_counts = spy_gate(env, mode=condition if seam == "gate" else None)
    ledger_counts = spy_ledger(env, mode=condition if seam == "W2" else None)
    receipts_fault = (seam, condition) if seam in ("W1", "S9", "S10", "W3") else None
    hooks = {}
    if seam == "S9":                       # HALT clear at S5, active at S9 (HALT #2)
        hooks["before_connect_2"] = lambda: setattr(adapter.test_halt, "state", "halted")
    if seam == "S10":                      # evidence verified pre-arm, rejected at the gate after A
        hooks["before_connect_2"] = lambda: setattr(env.gate.evidence_verifier, "verdict", "rejected")
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
    # P-9 (6870819489 / 6871180075): the legacy child is refused by the constructor
    # marker guard before accept(); supersedes the former "Fail-stop active" stage.
    assert legacy_report["result"] == "refused" and "signed_admission_marker" in legacy_report["message"]
    assert legacy_report["message"].startswith("Legacy constructor refused: ")
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


# =============================================================================
# Work order 6869856724 item 1 - provisioning outcome classification
# (real SQLite through the private _provision_connect seam)
# =============================================================================


class _ProvisionFault:
    """Wraps a real provisioning connection and injects one scripted fault."""

    def __init__(self, real, mode, log):
        self._real, self._mode, self._log = real, mode, log

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self._log.append(("execute", flat))
        if self._mode == "config" and flat.upper().startswith("PRAGMA JOURNAL_MODE"):
            raise InjectedFault("injected configuration failure")
        if self._mode == "begin" and flat.startswith("BEGIN"):
            raise InjectedFault("injected BEGIN failure before delegating")
        if (self._mode in ("rollback_returns", "rollback_raises")
                and flat.startswith("INSERT INTO signed_admission_marker")):
            raise InjectedFault("injected in-transaction failure before commit")
        return self._real.execute(sql, params)

    def commit(self):
        self._log.append(("commit", None))
        if self._mode == "commit_raises":
            raise InjectedFault("injected commit failure before delegating")
        self._real.commit()
        if self._mode == "commit_then_raise":
            raise InjectedFault("injected failure after real COMMIT")

    def rollback(self):
        self._log.append(("rollback", None))
        if self._mode == "rollback_raises":
            raise InjectedFault("injected rollback failure")
        return self._real.rollback()

    def close(self):
        self._log.append(("close", None))
        return self._real.close()


def _store_objects(path):
    con = _ro(path)
    try:
        names = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        marker = (con.execute("SELECT marker FROM signed_admission_marker").fetchall()
                  if "signed_admission_marker" in names else None)
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()
    return names, marker, mode


PROVISION_FAULTS = {
    # mode: (classification, rollback invoked, commit invoked, landed)
    "connect": ("pre_transaction_failure", False, False, False),
    "config": ("pre_transaction_failure", False, False, False),
    "begin": ("pre_transaction_failure", False, False, False),
    "rollback_returns": ("confirmed_rolled_back", True, False, False),
    "rollback_raises": ("ambiguous", True, False, False),
    "commit_raises": ("ambiguous", False, True, False),
    "commit_then_raise": ("ambiguous", False, True, True),
}


@pytest.mark.parametrize("mode", list(PROVISION_FAULTS))
def test_provisioning_fault_classification_never_claims_unobserved_rollback(tmp_path, monkeypatch, mode):
    classification, rollback_called, commit_called, landed = PROVISION_FAULTS[mode]
    target = tmp_path / "receipts.db"
    log = []
    real_connect = adapter_module._provision_connect

    def connect(uri):
        log.append(("connect", None))
        if mode == "connect":
            raise sqlite3.OperationalError("injected connection failure")
        return _ProvisionFault(real_connect(uri), mode, log)

    monkeypatch.setattr(adapter_module, "_provision_connect", connect)
    with pytest.raises(SignedAdmissionError) as info:
        provision_signed_receipts_store(target)
    error = info.value
    assert (error.classification, error.step) == (classification, "provision")
    events = [event for event, _ in log]
    assert events.count("rollback") == (1 if rollback_called else 0)
    assert events.count("commit") == (1 if commit_called else 0)
    assert mode == "connect" or events[-1] == "close"           # connection always cleaned up
    if classification != "confirmed_rolled_back":
        assert "rolled back" not in str(error) and "confirmed" not in str(error)
    # Observer (separate read-only connection): side effects reported, never removed.
    assert target.exists()
    if mode == "connect":
        assert target.stat().st_size == 0
    else:
        names, marker, journal = _store_objects(target)
        if landed:
            assert marker == [(SIGNED_MARKER_VALUE,)] and "fail_stop" in names
        else:
            assert names == [] and marker is None
        if mode != "config":
            assert journal == "delete"                           # D1 applied before BEGIN
    # Never overwrites: a second provisioning attempt refuses the existing file.
    monkeypatch.undo()
    with pytest.raises(SignedAdmissionError, match="target exists"):
        provision_signed_receipts_store(target)


def test_provisioning_unfaulted_control_through_the_same_seam(tmp_path, monkeypatch):
    log = []
    real_connect = adapter_module._provision_connect
    monkeypatch.setattr(adapter_module, "_provision_connect",
                        lambda uri: _ProvisionFault(real_connect(uri), None, log))
    config = provision_signed_receipts_store(tmp_path / "receipts.db")
    assert config["journal_mode"] == "delete"
    assert [e for e, _ in log if e in ("commit", "rollback", "close")] == ["commit", "close"]
    assert _store_objects(tmp_path / "receipts.db")[1] == [(SIGNED_MARKER_VALUE,)]


# =============================================================================
# Item 2 - pre-arm assignment evidence (S6(f), ruling C)
# =============================================================================

GATE_CALL = ("chief_authorization", ASSIGNMENT, BASE_SHA, "fixture-assign", NOW)

PREARM_DENIALS = {
    "trusted_fixture_false": ({"trusted_fixture": False}, None, {}, 0,
                              "Assignment requires trusted fixture"),
    "invalid_tuple_recipient": ({}, None, {"recipient": "skippy"}, 0,
                                "Assignment authorization tuple not permitted"),
    "evidence_id_missing": ({}, None, {"payload_drop": ("evidence_id",)}, 0,
                            "Assignment evidence input invalid: missing or empty evidence_id"),
    "evidence_id_wrong_type": ({}, None, {"payload_set": {"evidence_id": 7}}, 0,
                               "Assignment evidence input invalid: missing or empty evidence_id"),
    "evidence_id_whitespace": ({}, None, {"payload_set": {"evidence_id": " fixture-assign"}}, 0,
                               "Assignment evidence input invalid: evidence_id has surrounding whitespace"),
    "base_sha_missing": ({}, None, {"payload_drop": ("base_sha",)}, 0,
                         "Assignment evidence input invalid: invalid base_sha"),
    "base_sha_wrong_type": ({}, None, {"payload_set": {"base_sha": 12345}}, 0,
                            "Assignment evidence input invalid: invalid base_sha"),
    "base_sha_uppercase": ({}, None, {"payload_set": {"base_sha": BASE_SHA.upper()}}, 0,
                           "Assignment evidence input invalid: invalid base_sha"),
    "forbidden_implementation_sha": ({}, None, {"payload_set": {"implementation_sha": IMPL_SHA}}, 0,
                                     "Assignment evidence input invalid: forbidden field: implementation_sha"),
    "sha_alias": ({}, None, {"payload_set": {"sha": BASE_SHA}}, 0,
                  "Assignment evidence input invalid: adapter sha alias not accepted"),
    "dependency_rejected": ({}, "rejected", {}, 1, "Assignment evidence rejected"),
    "dependency_unavailable": ({}, "unavailable", {}, 1, "Assignment evidence unavailable"),
    "dependency_raises": ({}, "raise", {}, 1, "Assignment evidence verification failed closed"),
    "dependency_non_string": ({}, True, {}, 1, "Assignment evidence indeterminate"),
    "dependency_unknown_evidence_id": ({}, None, {"payload_set": {"evidence_id": "other-evidence"}}, 1,
                                       "Assignment evidence rejected"),
}


@pytest.mark.parametrize("case", list(PREARM_DENIALS))
def test_prearm_assignment_evidence_denial_never_arms(tmp_path, case):
    adapter_kwargs, verdict, overrides, evidence_calls, reason = PREARM_DENIALS[case]
    env = Env(tmp_path)
    adapter = env.adapter(**adapter_kwargs)
    control = env.adapter()
    if verdict is not None:
        env.gate.evidence_verifier.verdict = verdict
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    script = install_script(adapter, Script())
    evidence = spy_evidence(env, script.log)
    item = spec(**overrides)
    before = durable_state(env.paths)
    result, error = run(adapter, env, item)
    assert (result, error.step, error.poisoned) == ("rejected", "E1.b", False)
    assert len(evidence) == evidence_calls
    if evidence_calls:
        expected = list(GATE_CALL)
        expected[3] = item["payload_set"].get("evidence_id", "fixture-assign")
        assert evidence == [tuple(expected)]
    assert gate_counts == {"reserve": 0} and ledger_counts == {"record_event": 0}
    assert script.g_values == [] and [d for _, e, d in script.log if e == "commit"] == ["E1.b"]
    assert not any(s.startswith(("UPDATE fail_stop", "INSERT INTO intent_journal")) for s in statements(script))
    after = durable_state(env.paths)
    assert {k: v for k, v in after.items() if k != "audit_receipts"} == {
        k: v for k, v in before.items() if k != "audit_receipts"}
    assert [(r[1], r[8], r[9]) for r in after["audit_receipts"]] == [(item["mid"], "rejected", reason)]
    assert released(after) and after["reservations"] == [] and after["intent_journal"] == []
    # Valid control on the same store (rejected receipts do not trigger Rule U).
    env.gate.evidence_verifier.verdict = "verified"
    assert run(control, env, spec(tag="control")) == ("accepted", None)


def test_prearm_valid_control_calls_the_same_mapping_before_a_and_again_at_the_gate(tmp_path):
    env = Env(tmp_path)
    adapter = env.adapter()
    gate_counts = spy_gate(env)
    script = install_script(adapter, Script())
    evidence = spy_evidence(env, script.log)
    assert run(adapter, env, spec()) == ("accepted", None)
    assert evidence == [GATE_CALL, GATE_CALL] and gate_counts == {"reserve": 1}
    order = [(e, d) for _, e, d in script.log if e in ("evidence", "commit")]
    assert order == [("evidence", GATE_CALL[:4]), ("commit", "A"), ("evidence", GATE_CALL[:4]),
                     ("commit", "W1"), ("commit", "W3"), ("commit", "R")]
    # Non-assignment types keep their existing boundary: evidence only at the gate.
    evidence.clear()
    assert run(adapter, env, spec("implementation")) == ("accepted", None)
    assert evidence == [("chief_authorization", ASSIGNMENT, IMPL_SHA, "fixture-impl", NOW)]


@pytest.mark.parametrize("verdict", ["verified", "rejected", "raise"])
def test_bad_signature_is_zero_write_with_zero_evidence_arm_and_reserve_calls(tmp_path, verdict):
    env = Env(tmp_path)
    adapter = env.adapter()
    env.gate.evidence_verifier.verdict = verdict
    gate_counts, ledger_counts = spy_gate(env), spy_ledger(env)
    script = install_script(adapter, Script())
    evidence = spy_evidence(env)
    before = durable_state(env.paths)
    result, error = run(adapter, env, spec(bad_signature=True))
    assert result == "refused" and "invalid signature" in str(error) and not error.poisoned
    assert evidence == [] and script.g_values == [] and gate_counts == {"reserve": 0}
    assert ledger_counts == {"record_event": 0} and durable_state(env.paths) == before
    assert [d for _, e, d in script.log if e == "commit"] == []


# =============================================================================
# Item 3a - exact read-only reservation projection (T38)
# =============================================================================


def _tamper(column, value):
    return [(f"UPDATE reservations SET {column} = ? WHERE message_id = ?", "value", value)]


READBACK_MUTATIONS = {
    **{f"field_{c}": [(f"UPDATE reservations SET {c} = {c} || '-x' WHERE message_id = ?", "mid", None)]
       for c in ("project_id", "task_id", "checkpoint_id", "assignment_id", "message_id",
                 "idempotency_key", "sender", "recipient", "role")},
    "field_payload_digest": _tamper("payload_digest", bytes(32)),
    "field_sequence": _tamper("sequence", 2),
    "type_sequence_text": _tamper("sequence", "one"),
    "type_payload_digest_text": _tamper("payload_digest", "00" * 32),
    "type_sender_blob": _tamper("sender", b"chief"),
    "state_pending": _tamper("state", "PENDING"),
    "state_lowercase": _tamper("state", "reserved"),
    "id_zero": _tamper("id", 0),
    "id_negative": _tamper("id", -3),
    "recorded_at_not_iso": _tamper("recorded_at", "not-a-time"),
    "recorded_at_naive": _tamper("recorded_at", "2026-11-13T12:00:00"),
    "recorded_at_non_utc": _tamper("recorded_at", "2026-11-13T12:00:00+01:00"),
    "recorded_at_blob": _tamper("recorded_at", b"2026-11-13T12:00:00+00:00"),
    "cardinality_zero": [("DELETE FROM reservations WHERE message_id = ?", "mid", None)],
    "cardinality_two": [
        ("UPDATE reservations SET idempotency_key = 'j6-idem-other' WHERE message_id = ?", "mid", None),
        ("INSERT INTO reservations (project_id, task_id, checkpoint_id, assignment_id, message_id, "
         "idempotency_key, sender, recipient, role, payload_digest, sequence, state, recorded_at) "
         "SELECT project_id, task_id, checkpoint_id, assignment_id, 'j6-mid-other', ?, sender, "
         "recipient, role, payload_digest, sequence, state, recorded_at FROM reservations "
         "WHERE idempotency_key = 'j6-idem-other'", "idem", None),
    ],
}


@pytest.mark.parametrize("case", list(READBACK_MUTATIONS))
def test_readback_projection_mutation_fails_before_w1_and_keeps_g(tmp_path, case):
    env = Env(tmp_path)
    adapter = env.adapter()
    seen = {}

    def tamper(snap):
        statements_ = []
        for sql, kind, value in READBACK_MUTATIONS[case]:
            param = {"mid": snap.message_id, "idem": snap.idempotency_key}.get(kind)
            statements_.append((sql, (param,) if kind != "value" else (value, snap.message_id)))
        raw_write(env.admission, *statements_)
        seen["tampered"] = durable_state(env.paths)

    gate_counts = spy_gate(env, after=tamper)
    ledger_counts = spy_ledger(env)
    script = install_script(adapter, Script())
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("refused", "S11", True), (result, error)
    after = durable_state(env.paths)
    assert after == seen["tampered"]                          # read-back wrote nothing
    assert after["fail_stop"] == [(1, 1, script.g_values[0])] and after["intent_journal"] == []
    assert gate_counts == {"reserve": 1} and ledger_counts == {"record_event": 0}
    assert script.calls == 2 and [d for _, e, d in script.log if e == "commit"] == ["A"]
    expected_rows = 0 if case == "cardinality_zero" else (2 if case == "cardinality_two" else 1)
    assert len(after["reservations"]) == expected_rows


class _ReadbackShim:
    """In-module seam for read-back cases the frozen gate schema cannot hold."""

    def __init__(self, admission, mode):
        self._admission = str(Path(admission).resolve().as_uri())
        self._mode = mode
        self._real = sqlite3.connect

    def connect(self, target, *args, **kwargs):
        if isinstance(target, str) and target.startswith(self._admission):
            if self._mode == "open_failure":
                raise sqlite3.OperationalError("injected mode=ro open failure")
            return _RowRewrite(self._real(target, *args, **kwargs))
        return self._real(target, *args, **kwargs)


class _RowRewrite:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, params=()):
        cursor = self._real.execute(sql, params)
        if "FROM reservations" in sql:
            rows = [("7",) + tuple(row[1:]) for row in cursor.fetchall()]
            return _Rows(rows)
        return cursor

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


@pytest.mark.parametrize("mode", ["id_as_str", "open_failure"])
def test_readback_shimmed_cases_fail_before_w1_and_keep_g(tmp_path, monkeypatch, mode):
    env = Env(tmp_path)
    adapter = env.adapter()
    shim = _ReadbackShim(env.admission, mode)
    module_shim = type(sqlite3)("sqlite3_shim")
    module_shim.__dict__.update({k: v for k, v in vars(sqlite3).items() if not k.startswith("__")})
    module_shim.connect = shim.connect
    monkeypatch.setattr(adapter_module, "sqlite3", module_shim)
    ledger_counts = spy_ledger(env)
    script = install_script(adapter, Script())
    result, error = run(adapter, env, spec())
    assert (result, error.step, error.poisoned) == ("refused", "S11", True), (result, error)
    after = durable_state(env.paths)
    assert after["fail_stop"] == [(1, 1, script.g_values[0])] and after["intent_journal"] == []
    assert len(after["reservations"]) == 1 and ledger_counts == {"record_event": 0}


def test_readback_valid_control_leaves_reservation_rows_unchanged(tmp_path):
    env = Env(tmp_path)
    adapter = env.adapter()
    seen = {}
    spy_gate(env, after=lambda snap: seen.update(rows=durable_state(env.paths)["reservations"]))
    assert run(adapter, env, spec()) == ("accepted", None)
    assert durable_state(env.paths)["reservations"] == seen["rows"]


def test_reservations_schema_persists_no_signed_bytes_documentation():
    """The read-back is stored-row equality only: these values are never persisted,
    so no claim about reconstructed signature authenticity is possible."""
    import skippy.collaboration.offline_admission_gate as gate_module
    con = sqlite3.connect(":memory:")
    try:
        con.execute(gate_module._TABLE_DDL)
        columns = {row[1] for row in con.execute("PRAGMA table_info(reservations)")}
    finally:
        con.close()
    assert not columns & {"signature", "key_id", "schema_version", "timestamp", "expiry", "payload"}


# =============================================================================
# Item 3b - signed-record shape and marker validation (T17, T39)
# =============================================================================

import json as _json

A_MID, I_MID = spec("assignment")["mid"], spec("implementation")["mid"]


def _signed_history(tmp_path):
    env = Env(tmp_path)
    adapter = env.adapter()
    assert run(adapter, env, spec("assignment")) == ("accepted", None)
    assert run(adapter, env, spec("implementation")) == ("accepted", None)
    return env, adapter


def _rewrite(env, mid, mutate=None, actor=None, raw=None):
    con = sqlite3.connect(env.ledger_path)
    try:
        payload = _json.loads(con.execute("SELECT payload FROM events WHERE message_id = ?", (mid,)).fetchone()[0])
        if mutate is not None:
            mutate(payload)
        text = raw(payload) if raw else _json.dumps(payload, sort_keys=True, separators=(",", ":"))
        con.execute("UPDATE events SET payload = ? WHERE message_id = ?", (text, mid))
        if actor is not None:
            con.execute("UPDATE events SET actor = ? WHERE message_id = ?", (actor, mid))
        con.commit()
    finally:
        con.close()


def _drop(key):
    return lambda p: p.pop(key)


def _set(key, value):
    return lambda p: p.__setitem__(key, value)


ASSIGNMENT_KEYS = ("synthetic", "schema_version", "digest_kind", "type", "assignment_id", "recipient",
                   "role", "evidence_kind", "evidence_id", "base_sha")
SIGNED_RECORD_REFUSALS = {
    **{f"missing_{k}": (A_MID, _drop(k), None, "closed key set") for k in ASSIGNMENT_KEYS
       if k not in ("digest_kind", "type", "assignment_id")},
    # assignment_id is a legacy stored-equality field: legacy reconcile refuses first.
    "missing_assignment_id": (A_MID, _drop("assignment_id"), None, "Identity mismatch"),
    "missing_type": (A_MID, _drop("type"), None, "type"),
    "missing_digest_kind": (A_MID, _drop("digest_kind"), None, "digest_kind"),
    "missing_digest": (A_MID, _drop("digest"), None, "Identity mismatch"),
    "missing_adapter_origin": (A_MID, _drop("adapter_origin"), None, "ACCEPTED intent without matching ledger event"),
    "missing_sha_on_implementation": (I_MID, _drop("sha"), None, "closed key set"),
    "extra_key": (A_MID, _set("verdict", "approved"), None, "closed key set"),
    "extra_both_sha_fields": (A_MID, _set("implementation_sha", IMPL_SHA), None, "closed key set"),
    "extra_base_sha_on_implementation": (I_MID, _set("base_sha", BASE_SHA), None, "closed key set"),
    "type_synthetic_int": (A_MID, _set("synthetic", 1), None, "synthetic"),
    "type_schema_version_str": (A_MID, _set("schema_version", "1"), None, "schema_version"),
    "type_schema_version_bool": (A_MID, _set("schema_version", True), None, "schema_version"),
    "type_assignment_id_int": (A_MID, _set("assignment_id", 5), None, "Identity mismatch"),
    "empty_role": (A_MID, _set("role", ""), None, "role"),
    "empty_evidence_id": (A_MID, _set("evidence_id", ""), None, "evidence_id"),
    "type_evidence_kind_int": (A_MID, _set("evidence_kind", 3), None, "evidence_kind"),
    "base_sha_uppercase": (A_MID, _set("base_sha", BASE_SHA.upper()), None, "base_sha"),
    "implementation_sha_short": (I_MID, _set("implementation_sha", IMPL_SHA[:39]), None, "implementation_sha"),
    "mapping_role": (A_MID, _set("role", "implementer"), None, "authorization tuple"),
    "mapping_recipient": (A_MID, _set("recipient", "skippy"), None, "authorization tuple"),
    "mapping_evidence_kind": (A_MID, _set("evidence_kind", "skippy_review"), None, "authorization tuple"),
    "mapping_type_event": (I_MID, _set("type", "review"), None, "event mapping"),
    "mapping_type_unknown": (A_MID, _set("type", "bogus"), None, "type"),
    "mapping_type_keyset": (I_MID, _set("type", "assignment"), None, "closed key set"),
    "derived_sha_mismatch": (I_MID, _set("sha", BASE_SHA), None, "derived sha"),
    "derived_from_wrong": (I_MID, _set("sha_derived_from", "base_sha"), None, "derived sha"),
    "marker_admission_path": (A_MID, _set("admission_path", "bridge-02b-other"), None, "admission_path"),
    "marker_digest_kind": (A_MID, _set("digest_kind", "sha256"), None, "digest_kind"),
    "marker_adapter_origin": (A_MID, _set("adapter_origin", "bridge-02x"), None,
                              "ACCEPTED intent without matching ledger event"),
    "fabricated_marker_wrong_digest": (A_MID, _set("digest", "ab" * 32), None, "Identity mismatch"),
    "fabricated_marker_wrong_actor": (A_MID, None, "jason", "Sender mismatch"),
}


@pytest.mark.parametrize("case", list(SIGNED_RECORD_REFUSALS))
def test_signed_record_shape_and_marker_violation_refuses(tmp_path, case):
    mid, mutate, actor, needle = SIGNED_RECORD_REFUSALS[case]
    env, earlier = _signed_history(tmp_path)
    _rewrite(env, mid, mutate=mutate, actor=actor)
    before = durable_state(env.paths)
    with pytest.raises(SignedAdmissionError) as info:
        env.adapter()
    assert info.value.classification == "refused" and needle in str(info.value), str(info.value)
    gate_counts = spy_gate(env)
    result, error = run(earlier, env, spec("review"))
    assert result == "refused" and error.step == "T1" and needle in str(error)
    assert gate_counts == {"reserve": 0} and durable_state(env.paths) == before


SIGNED_RECORD_ACCEPTED = {
    # T17 group 3: shape-only, no verification claim.
    "tampered_evidence_id_shape_valid": (A_MID, _set("evidence_id", "fixture-other"), None),
    # Semantic JSON round trip is not signed-byte preservation.
    "json_reserialized": (A_MID, None, lambda p: _json.dumps(p, indent=2)),
    # T39(d): a marker removed from a signed record leaves legacy rules only (undetectable).
    "marker_removed_admission_path": (A_MID, _drop("admission_path"), None),
}


@pytest.mark.parametrize("case", list(SIGNED_RECORD_ACCEPTED))
def test_signed_record_documented_shape_only_limits(tmp_path, case):
    mid, mutate, raw = SIGNED_RECORD_ACCEPTED[case]
    env, _ = _signed_history(tmp_path)
    _rewrite(env, mid, mutate=mutate, raw=raw)
    env.adapter()                                              # construction passes: shape-only check


# =============================================================================
# P-9 legacy-constructor guard (work order 6870819489, amended by 6871180075)
# =============================================================================
#
# Fresh disposable stores only. "Baseline" controls reproduce the pre-P-9
# (6e9d453) ``_init_receipts`` transaction pattern test-locally on their own
# disposable store: the same statements through a default-isolation
# connection used as a context manager, so each CREATE autocommits and the
# INSERT commits when the block exits. No frozen repository file and no live
# store is used to make a control. Snapshots are taken after setup, when the
# store is quiescent; file hashes are asserted only for rollback-journal
# (DELETE) stores, because WAL mode adds -wal/-shm housekeeping that is not a
# logical write.

P9_PREFIX = "Legacy constructor refused: "
P9_MARKER = P9_PREFIX + "receipts store carries reserved schema object signed_admission_marker"
P9_WRITE_VERBS = ("CREATE", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "REPLACE")


def _p9_baseline_init(path, fail_after=None):
    """Pre-P-9 pattern: per-statement autocommitted DDL, implicit INSERT transaction."""
    con = sqlite3.connect(path)
    try:
        with con:
            for statement in adapter_module._LEGACY_INIT_STATEMENTS:
                con.execute(statement)
                if fail_after and " ".join(statement.split()).startswith(fail_after):
                    raise InjectedFault(f"baseline injected after {fail_after}")
    finally:
        con.close()


def _p9_snapshot(path):
    """Every schema object and every row of every table (read-only connection)."""
    con = _ro(path)
    try:
        objects = con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        rows = {}
        for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall():
            rows[name] = con.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
    finally:
        con.close()
    return {"objects": objects, "rows": rows}


def _p9_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _p9_legacy(ledger, path):
    return OfflineAuthenticatedAdapter(ledger, path, trusted_fixture=True, halt_check=lambda: False)


def _p9_writes(log):
    return [flat for flat in log if flat.split(" ", 1)[0].upper() in P9_WRITE_VERBS]


class _P9Recorder:
    """Test-local proxy around the private P-9 seam ``_legacy_init_connect``.

    Records every statement and call. Optional faults: ``after`` raises
    InjectedFault after the real statement with that prefix executed;
    ``rollback`` makes rollback() raise without delegating; ``commit`` makes
    commit() raise without delegating.
    """

    def __init__(self, real, fault=None):
        self._real, self._fault, self.log = real, dict(fault or {}), []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.log.append(flat)
        cursor = self._real.execute(sql, params)
        if self._fault.get("after") and flat.startswith(self._fault["after"]):
            raise InjectedFault(f"injected after {self._fault['after']}")
        return cursor

    def commit(self):
        self.log.append("commit()")
        if self._fault.get("commit"):
            raise InjectedFault("injected commit failure (not delegated)")
        return self._real.commit()

    def rollback(self):
        self.log.append("rollback()")
        if self._fault.get("rollback"):
            raise InjectedFault("injected rollback failure (not delegated)")
        return self._real.rollback()

    def close(self):
        self.log.append("close()")
        return self._real.close()


def _p9_spy(monkeypatch, fault=None):
    """Record seam connections and any later constructor step (_schema_preflight, _reconcile)."""
    made, calls = [], []
    real_factory = adapter_module._legacy_init_connect

    def factory(path):
        recorder = _P9Recorder(real_factory(path), fault)
        made.append(recorder)
        return recorder

    monkeypatch.setattr(adapter_module, "_legacy_init_connect", factory)
    for name in ("_schema_preflight", "_reconcile"):
        original = getattr(OfflineAuthenticatedAdapter, name)

        def wrapper(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(OfflineAuthenticatedAdapter, name, wrapper)
    return made, calls


def _p9_assert_refused_without_writes(made, calls):
    assert calls == [], "a later constructor step ran after the guard refused"
    assert len(made) == 1
    log = made[0].log
    assert log[0] == "BEGIN IMMEDIATE", log           # lock first, before any read or write
    assert _p9_writes(log) == [], log
    assert "commit()" not in log and log[-1] == "close()", log


def test_p9_guarded_statements_are_the_unchanged_legacy_statements():
    statements = adapter_module._LEGACY_INIT_STATEMENTS
    ddl = [s for s in statements if not s.startswith("INSERT")]
    # _SIGNED_RECEIPTS_DDL is documented as the byte-for-byte legacy DDL plus the marker table.
    assert ddl == list(adapter_module._SIGNED_RECEIPTS_DDL[:-1])
    assert [s for s in statements if s.startswith("INSERT")] == [
        "INSERT OR IGNORE INTO fail_stop (singleton, stopped, reason) VALUES (1, 0, NULL)"]
    assert statements.index(next(s for s in statements if s.startswith("INSERT"))) == 4


P9_DAMAGE = {
    "deleted_fail_stop": [("DELETE FROM fail_stop", ())],
    "dropped_receipts_table": [("DROP TABLE sequence_state", ())],
    "dropped_audit_receipts": [("DROP TABLE audit_receipts", ())],
    "dropped_trigger": [("DROP TRIGGER audit_receipts_no_delete", ())],
}


@pytest.mark.parametrize("case", list(P9_DAMAGE))
def test_p9_damaged_marked_store_baseline_repairs_but_guard_refuses_without_writes(tmp_path, monkeypatch,
                                                                                   case):
    # Isolated baseline control on its own disposable store.
    base = Env(tmp_path / "baseline")
    raw_write(base.receipts, *P9_DAMAGE[case])
    damaged = _p9_snapshot(base.receipts)
    with pytest.raises(SignedAdmissionError):
        base.adapter()
    _p9_baseline_init(base.receipts)
    repaired = _p9_snapshot(base.receipts)
    assert repaired != damaged                                  # old initialization repaired evidence
    if case == "deleted_fail_stop":
        assert repaired["rows"]["fail_stop"] == [(1, 0, None)]
    base.adapter()                                              # ...and the store became admissible
    # Corrected constructor on a separate, identically damaged disposable store.
    env = Env(tmp_path / "guarded")
    raw_write(env.receipts, *P9_DAMAGE[case])
    before, digest = _p9_snapshot(env.receipts), _p9_sha(env.receipts)
    files = sorted(os.listdir(env.root))
    made, calls = _p9_spy(monkeypatch)
    with pytest.raises(OfflineAdapterError, match="signed_admission_marker") as info:
        _p9_legacy(env.ledger, env.receipts)
    assert str(info.value).startswith(P9_MARKER) and "rollback returned" in str(info.value)
    _p9_assert_refused_without_writes(made, calls)
    assert _p9_snapshot(env.receipts) == before and _p9_sha(env.receipts) == digest
    assert sorted(os.listdir(env.root)) == files
    if case == "deleted_fail_stop":
        assert before["rows"]["fail_stop"] == []                # remains deleted
    with pytest.raises(SignedAdmissionError) as refused:
        env.adapter()                                           # open_signed still refuses
    assert refused.value.classification == "refused"


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
@pytest.mark.parametrize("stop", ["healthy", "armed"])
def test_p9_healthy_and_armed_marked_stores_unchanged_and_g_retained(tmp_path, monkeypatch, journal_mode,
                                                                      stop):
    env = Env(tmp_path, journal_mode=journal_mode)
    g_value = "p9-synthetic-g-" + "5a" * 16
    if stop == "armed":
        raw_write(env.receipts, ("UPDATE fail_stop SET stopped = 1, reason = ? WHERE singleton = 1",
                                 (g_value,)))
    before, snapshot = durable_state(env.paths), _p9_snapshot(env.receipts)
    digest = _p9_sha(env.receipts)
    made, calls = _p9_spy(monkeypatch)
    with pytest.raises(OfflineAdapterError, match="signed_admission_marker") as info:
        _p9_legacy(env.ledger, env.receipts)
    assert str(info.value).startswith(P9_MARKER) and g_value not in str(info.value)
    _p9_assert_refused_without_writes(made, calls)
    assert durable_state(env.paths) == before and _p9_snapshot(env.receipts) == snapshot
    if journal_mode == "delete":
        assert _p9_sha(env.receipts) == digest
    if stop == "armed":
        assert before["fail_stop"] == [(1, 1, g_value)]
        with pytest.raises(SignedAdmissionError, match="stop present"):
            env.adapter()
    else:
        assert before["fail_stop"] == [(1, 0, None)]
        env.adapter()                                           # healthy store still admissible
    assert durable_state(env.paths) == before


P9_RESERVED = {
    # case: (provisioned first?, setup statements)
    "empty_marker": (True, [("DELETE FROM signed_admission_marker", ())]),
    "malformed_marker": (True, [("UPDATE signed_admission_marker SET marker = 'not-the-marker'", ())]),
    "marker_replaced_by_view": (True, [("DROP TABLE signed_admission_marker", ()),
                                       ("CREATE VIEW signed_admission_marker AS SELECT 1 AS singleton", ())]),
    "view_on_unprovisioned_file": (False, [("CREATE VIEW signed_admission_marker AS SELECT 1 AS x", ())]),
    "index_on_unprovisioned_file": (False, [("CREATE TABLE unrelated (x)", ()),
                                            ("CREATE INDEX signed_admission_marker ON unrelated (x)", ())]),
    "trigger_on_unprovisioned_file": (False, [
        ("CREATE TABLE unrelated (x)", ()),
        ("CREATE TRIGGER signed_admission_marker AFTER INSERT ON unrelated BEGIN SELECT 1; END", ())]),
    "case_variant_table_on_unprovisioned_file": (False, [("CREATE TABLE SIGNED_ADMISSION_MARKER (x)", ())]),
}


@pytest.mark.parametrize("case", list(P9_RESERVED))
def test_p9_any_reserved_marker_object_refuses_before_init_writes(tmp_path, monkeypatch, case):
    provisioned, setup = P9_RESERVED[case]
    env = Env(tmp_path, provision=provisioned)
    raw_write(env.receipts, *setup)
    before, digest = _p9_snapshot(env.receipts), _p9_sha(env.receipts)
    made, calls = _p9_spy(monkeypatch)
    with pytest.raises(OfflineAdapterError, match="signed_admission_marker") as info:
        _p9_legacy(env.ledger, env.receipts)
    assert str(info.value).startswith(P9_MARKER)
    _p9_assert_refused_without_writes(made, calls)
    assert _p9_snapshot(env.receipts) == before and _p9_sha(env.receipts) == digest
    if not provisioned:
        assert "fail_stop" not in before["rows"]                # no legacy object was created
    with pytest.raises(SignedAdmissionError):
        env.adapter()


def test_p9_non_sqlite_bytes_and_unopenable_paths_refuse_unchanged(tmp_path, monkeypatch):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    junk = tmp_path / "junk.db"
    payload = b"not a sqlite store" * 64
    junk.write_bytes(payload)
    folder = tmp_path / "a-directory"
    folder.mkdir()
    files = sorted(os.listdir(tmp_path))
    made, calls = _p9_spy(monkeypatch)
    for target in (junk, folder):
        with pytest.raises(OfflineAdapterError, match="^" + P9_PREFIX) as info:
            _p9_legacy(ledger, target)
        assert "no initialization write issued" in str(info.value)
        assert isinstance(info.value.__cause__, sqlite3.Error), repr(info.value.__cause__)
    assert calls == []
    assert all(_p9_writes(recorder.log) == [] and recorder.log[-1] == "close()" for recorder in made)
    assert junk.read_bytes() == payload and sorted(os.listdir(tmp_path)) == files
    assert os.listdir(folder) == []


@pytest.mark.parametrize("also_deleted_stop", [False, True])
def test_p9_dropped_marker_is_an_undetected_limit_not_a_safe_store(tmp_path, also_deleted_stop):
    env = Env(tmp_path)
    statements = [("DROP TABLE signed_admission_marker", ())]
    if also_deleted_stop:
        statements.append(("DELETE FROM fail_stop", ()))
    raw_write(env.receipts, *statements)
    before = _p9_snapshot(env.receipts)
    _p9_legacy(env.ledger, env.receipts)                       # stated limit: indistinguishable from unmarked
    after = _p9_snapshot(env.receipts)
    if also_deleted_stop:
        assert after["rows"]["fail_stop"] == [(1, 0, None)]   # limit: repaired, as for any unmarked store
    else:
        assert after == before
    with pytest.raises(SignedAdmissionError, match="provisioning marker missing"):
        env.adapter()                                           # never treated as a safe signed store


def test_p9_missing_and_unmarked_existing_stores_keep_legacy_outcomes(tmp_path, monkeypatch):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    baseline = tmp_path / "baseline.db"
    _p9_baseline_init(baseline)
    made, calls = _p9_spy(monkeypatch)
    missing = tmp_path / "missing.db"
    adapter = _p9_legacy(ledger, missing)                      # missing path: created unmarked
    assert _p9_snapshot(missing) == _p9_snapshot(baseline)
    assert calls == ["_schema_preflight", "_reconcile"]
    log = made[0].log
    assert log[0] == "BEGIN IMMEDIATE" and log[-2:] == ["commit()", "close()"]
    envelope = build_envelope(
        project_id=PROJECT, task_id=TASK, checkpoint_id=CHECKPOINT, assignment_id=ASSIGNMENT,
        message_id="p9-legacy-1", idempotency_key="p9-legacy-idem-1", sender="chief", recipient="jason",
        role="chief", payload={"type": "assignment", "objective": "legacy"}, timestamp=NOW,
        expiry=NOW + timedelta(hours=1), sequence=1,
    )
    assert adapter.accept(envelope, now=NOW) == "accepted"
    populated, digest = _p9_snapshot(missing), _p9_sha(missing)
    _p9_legacy(ledger, missing)                                # existing initialized unmarked store
    assert _p9_snapshot(missing) == populated and _p9_sha(missing) == digest
    unrelated = tmp_path / "unrelated.db"
    raw_write(unrelated, ("CREATE TABLE unrelated (x)", ()), ("INSERT INTO unrelated VALUES (7)", ()))
    _p9_legacy(CheckpointLedger(tmp_path / "unrelated-ledger.db"), unrelated)  # unmarked, other objects
    snap = _p9_snapshot(unrelated)
    assert snap["rows"]["unrelated"] == [(7,)] and snap["rows"]["fail_stop"] == [(1, 0, None)]
    expected = {(kind, name) for kind, name, _, _ in _p9_snapshot(baseline)["objects"]}
    assert expected <= {(kind, name) for kind, name, _, _ in snap["objects"]}


P9_INIT_FAULTS = {
    # mode: (fault, expected text, text that must be absent)
    "statement_fails_rollback_returns": ({"after": "CREATE TABLE IF NOT EXISTS intent_journal"},
                                         "rollback returned; nothing committed by this call", None),
    "statement_fails_rollback_raises": ({"after": "CREATE TABLE IF NOT EXISTS intent_journal",
                                         "rollback": True},
                                        "rollback raised; outcome unknown", "rollback returned"),
    "commit_raises": ({"commit": True}, "commit outcome unknown", "rollback returned"),
}


@pytest.mark.parametrize("mode", list(P9_INIT_FAULTS))
def test_p9_injected_init_failure_is_atomic_and_never_overclaims_rollback(tmp_path, monkeypatch, mode):
    fault, needle, absent = P9_INIT_FAULTS[mode]
    if fault.get("after"):
        # Baseline control: the same failure point under the pre-P-9 pattern leaves partial DDL.
        base = tmp_path / "baseline.db"
        with pytest.raises(InjectedFault):
            _p9_baseline_init(base, fail_after=fault["after"])
        tables = {name for kind, name, _, _ in _p9_snapshot(base)["objects"] if kind == "table"}
        assert {"audit_receipts", "sequence_state", "intent_journal"} <= tables
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    target = tmp_path / "receipts.db"
    made, calls = _p9_spy(monkeypatch, fault)
    with pytest.raises(OfflineAdapterError) as info:
        _p9_legacy(ledger, target)
    message = str(info.value)
    assert needle in message, message
    if absent:
        assert absent not in message, message
    assert isinstance(info.value.__cause__, InjectedFault)
    assert calls == [] and made[0].log[0] == "BEGIN IMMEDIATE" and made[0].log[-1] == "close()"
    # Fresh read-only observation (observer knowledge, not the caller's classification).
    assert _p9_snapshot(target)["objects"] == []
    monkeypatch.undo()
    _p9_legacy(ledger, target)                                 # a later unfaulted construction succeeds
    assert _p9_snapshot(target)["rows"]["fail_stop"] == [(1, 0, None)]


def test_p9_lock_timeout_refuses_before_any_write(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    target = tmp_path / "receipts.db"
    _p9_legacy(ledger, target)
    before, digest = _p9_snapshot(target), _p9_sha(target)
    holder = sqlite3.connect(target, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(OfflineAdapterError, match="^" + P9_PREFIX + "receipts lock not acquired") as info:
            _p9_legacy(ledger, target)
        waited = time.monotonic() - started
        assert "database is locked" in str(info.value) and isinstance(info.value.__cause__, sqlite3.Error)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert 4.0 <= waited < 30.0, waited                        # bounded by the legacy 5 s busy timeout
    assert _p9_snapshot(target) == before and _p9_sha(target) == digest


# --- P-9 two-process constructor/provisioning contention ---------------------

P9_EVENT_NAMES = ("ready", "go", "paused", "release", "begin_requested")


class _P9PausingConnection:
    """Child-local proxy: signals before BEGIN IMMEDIATE and pauses at one point."""

    def __init__(self, real, events, pause_on):
        self._real, self._events, self._pause_on = real, events, pause_on

    def _pause(self):
        self._events["paused"].set()
        if not self._events["release"].wait(START_TIMEOUT):
            raise Inconclusive("release not received")

    def execute(self, sql, params=()):
        if " ".join(sql.split()).upper().startswith("BEGIN IMMEDIATE"):
            if self._pause_on == "before_begin":
                self._pause()
            self._events["begin_requested"].set()
        return self._real.execute(sql, params)

    def commit(self):
        if self._pause_on == "before_commit":
            self._pause()
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _p9_child_main(job, conn, events):
    """Spawned P-9 participant with its own fresh connections; never re-raises."""
    report = {"pid": os.getpid(), "error": None, "role": job["role"]}
    try:
        path = Path(job["path"])
        if job["role"] == "provision":
            real_provision = adapter_module._provision_connect
            adapter_module._provision_connect = lambda uri: _P9PausingConnection(
                real_provision(uri), events, job["pause_on"])
            events["ready"].set()
            if not events["go"].wait(START_TIMEOUT):
                raise Inconclusive("go not received")
            try:
                provision_signed_receipts_store(path)
                report["result"] = "provisioned"
            except SignedAdmissionError as exc:
                report.update(result="refused", classification=exc.classification, message=str(exc))
        else:
            real_init = adapter_module._legacy_init_connect
            adapter_module._legacy_init_connect = lambda target: _P9PausingConnection(
                real_init(target), events, job["pause_on"])
            ledger = CheckpointLedger(Path(job["ledger"]))
            events["ready"].set()
            if not events["go"].wait(START_TIMEOUT):
                raise Inconclusive("go not received")
            try:
                _p9_legacy(ledger, path)
                report["result"] = "constructed"
            except OfflineAdapterError as exc:
                report.update(result="refused", message=str(exc))
    except BaseException:  # noqa: BLE001 - the parent receives the full traceback
        report["error"] = traceback.format_exc()
    try:
        conn.send(report)
    finally:
        conn.close()


class _P9Child:
    def __init__(self, job):
        self.events = {name: SPAWN.Event() for name in P9_EVENT_NAMES}
        self.receiver, sender = SPAWN.Pipe(duplex=False)
        self.process = SPAWN.Process(target=_p9_child_main, args=(job, sender, self.events), daemon=True)
        self.process.start()
        sender.close()
        self._report = None

    def wait(self, name, timeout=BARRIER_TIMEOUT):
        if not self.events[name].wait(timeout):
            raise Inconclusive(f"P-9 child event {name!r} not observed within {timeout}s")

    def set(self, name):
        self.events[name].set()

    def finished(self):
        return self.receiver.poll(0)

    def report(self, timeout=START_TIMEOUT):
        if self._report is None:
            if not self.receiver.poll(timeout):
                raise Inconclusive(f"P-9 child result not received within {timeout}s")
            self._report = self.receiver.recv()
            assert self._report["error"] is None, self._report["error"]
        return self._report

    def stop(self):
        if self.process.is_alive():
            for name in ("go", "release"):
                self.events[name].set()
            self.process.join(2.0)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(JOIN_TIMEOUT)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(JOIN_TIMEOUT)
        self.receiver.close()


@pytest.fixture
def p9_children():
    started = []

    def start(job):
        child = _P9Child(job)
        started.append(child)
        child.wait("ready", START_TIMEOUT)
        return child

    try:
        yield start
    finally:
        for child in started:
            child.stop()
        assert not any(child.process.is_alive() for child in started), "P-9 child process survived"


@pytest.mark.parametrize("schedule", ["provisioning_holds_lock_first", "constructor_holds_lock_first"])
def test_p9_two_process_constructor_and_provisioning_never_both_succeed(tmp_path, p9_children, schedule):
    target = tmp_path / "receipts.db"
    ledger = str(tmp_path / "ledger.db")
    if schedule == "provisioning_holds_lock_first":
        provisioner = p9_children({"role": "provision", "path": str(target), "pause_on": "before_commit"})
        provisioner.set("go")
        provisioner.wait("paused")             # P-3 holds BEGIN IMMEDIATE: schema + marker written, uncommitted
        constructor = p9_children({"role": "construct", "path": str(target), "ledger": ledger,
                                   "pause_on": None})
        constructor.set("go")
        constructor.wait("begin_requested")
        time.sleep(0.5)
        blocked = not constructor.finished()
        provisioner.set("release")
        waiter, waiter_report = constructor, constructor.report()
        holder_report = provisioner.report()
        if waiter_report["result"] == "refused" and "lock not acquired" in waiter_report["message"]:
            raise Inconclusive("constructor busy wait expired: inconclusive, not a pass")
    else:
        provisioner = p9_children({"role": "provision", "path": str(target), "pause_on": "before_begin"})
        provisioner.set("go")
        provisioner.wait("paused")             # P-3 created the empty file (O_EXCL); no transaction yet
        constructor = p9_children({"role": "construct", "path": str(target), "ledger": ledger,
                                   "pause_on": "before_commit"})
        constructor.set("go")
        constructor.wait("paused")             # constructor holds BEGIN IMMEDIATE: legacy DDL uncommitted
        provisioner.set("release")
        provisioner.wait("begin_requested")
        time.sleep(0.5)
        blocked = not provisioner.finished()
        constructor.set("release")
        holder_report = constructor.report()
        waiter, waiter_report = provisioner, provisioner.report()
        if (waiter_report["result"] == "refused"
                and waiter_report.get("classification") == "pre_transaction_failure"):
            raise Inconclusive("provisioning busy wait expired: inconclusive, not a pass")
    assert blocked, f"{waiter_report['role']} completed while the other participant held the receipts lock"
    outcomes = {holder_report["role"]: holder_report["result"], waiter_report["role"]: waiter_report["result"]}
    assert outcomes != {"provision": "provisioned", "construct": "constructed"}, "both succeeded"
    snapshot = _p9_snapshot(target)
    tables = {name for kind, name, _, _ in snapshot["objects"] if kind == "table"}
    con = _ro(target)
    try:
        schema_problem = adapter_module._signed_schema_problem(con)
    finally:
        con.close()
    if schedule == "provisioning_holds_lock_first":
        assert outcomes == {"provision": "provisioned", "construct": "refused"}
        assert waiter_report["message"].startswith(P9_MARKER)
        assert schema_problem is None
        assert [row[:2] for row in snapshot["rows"]["signed_admission_marker"]] == [(1, SIGNED_MARKER_VALUE)]
    else:
        assert outcomes == {"construct": "constructed", "provision": "refused"}
        assert waiter_report["classification"] == "confirmed_rolled_back"
        assert "not an empty new store" in waiter_report["message"]
        assert "signed_admission_marker" not in tables
    assert snapshot["rows"]["fail_stop"] == [(1, 0, None)]
    print(f"P9-OBSERVATION schedule={schedule} outcomes={outcomes} tables={sorted(tables)}")
