"""
Adapter reconciliation consistency tests (receipts.db + ledger.db).

Synthetic. Offline. Non-executing. execution_enabled stays False.

Contract under test (coordinated reads, not cross-database atomic commits):
- W1 PENDING commit -> W2 ledger event -> W3 ACCEPTED + receipt + sequence.
  PENDING stays durable before the ledger write.
- _reconcile collects its four receipts queries inside one receipts
  BEGIN IMMEDIATE transaction and reads the ledger view while that lock is
  still held. A concurrent admission is therefore seen as nothing, fully
  accepted, or PENDING (Unresolved); never as a torn cross-database view.
- Constructor path owns its receipts connection; accept() path borrows
  accept()'s open transaction and never begins, commits, rolls back or
  closes it.
- Existing corruption checks and messages are unchanged.

Scheduling rules: threading.Event pauses only, bounded waits and joins only,
every pause gate is released in ``finally``. No test waits for T1 to finish
while T2 holds the receipts lock; where a writer is expected to be blocked,
the test observes it for a bounded interval well below the 5 s busy timeout
and then releases T2. Separate-process tests use the ``spawn`` start method
only (Windows-safe, nothing inherited), multiprocessing Events, bounded
waits/joins, and release/terminate/join every child in ``finally``.

Mapping: originally approved requirement -> tests in this module
(test names keep their implementation numbering; ``orig_atN`` names cite the
original requirement id directly):

  orig AT-0  joint-consistency helper ... assert_joint_consistency, test_at0_*,
             applied after every concurrent case (incl. orig AT-15)
  orig AT-1  forced case A ............. test_at1_*, test_at2_*
  orig AT-2  forced case B ............. test_at3_*
  orig AT-3  forced case C ............. test_at4_*
  orig AT-4  reverse-direction guard ... test_at5_*
  orig AT-5  controls D1 / D2 .......... test_at1_* (D1), test_at6_* (D2)
  orig AT-6  real corruption, exact .... test_at7a..f_*, test_orig_at6_*
             messages                    (+ unchanged component tests)
  orig AT-7  PENDING durable first ..... test_at8_* (+ unchanged component tests)
  orig AT-8  reconcile inside admission  test_at9_*, test_orig_at8_*
             transaction
  orig AT-9  contention bound .......... test_orig_at15_* (blocked writer
             completes after release, no 'database is locked'), test_at2..5_*
  orig AT-10 Windows reliability ....... test_at10a_* in-suite; 200/1,000
             batches and full suite run outside this module
  orig AT-11 no weakening .............. procedural (protected-file hashes)
  orig AT-12/13/14 constructor and accept paths for lock contention,
             ledger-view failure and mid-reconciliation exception, ownership,
             closure, no unintended writes, fresh-writer progress
             ........................... test_at12a..d_*, test_at13a..c_*,
             test_at14a..b_*, test_at15_*, test_orig_at12_14_*
  orig AT-15 separate-process SQLite exclusion
             ........................... test_orig_at15_*
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

import skippy.collaboration.offline_authenticated_adapter as adapter_module
from skippy.collaboration.checkpoint_ledger import CheckpointLedger
from skippy.collaboration.offline_authenticated_adapter import (
    OfflineAdapterError,
    OfflineAuthenticatedAdapter,
)
from test_authenticated_envelope import _envelope, _now

TIMEOUT = 10.0           # bounded wait for events and joins
BLOCK_OBSERVE = 1.0      # bounded observation of an expected-blocked writer
ALLOWED = ("Sequence", "Replay", "Fail-stop", "PENDING", "Unresolved", "Active")
FALSE_CORRUPTION = (
    "ACCEPTED intent without matching ledger event",
    "Extra accepted receipt without journal intent",
    "Orphan sequence state",
    "Adapter ledger event without ACCEPTED intent",
)

SQL_UNRESOLVED = r"^SELECT message_id, state FROM intent_journal WHERE state IN"
SQL_RECEIPTS = r"^SELECT message_id, idempotency_key, project_id, task_id, checkpoint_id, assignment_id, sender FROM audit_receipts"
SQL_SEQSTATE = r"^SELECT stream_key, next_sequence FROM sequence_state$"
SQL_LEDGER_VIEW = r"^SELECT message_id, idempotency_key, project_id, task_id, checkpoint_id, actor, payload FROM events"
SQL_MARK_ACCEPTED = r"^UPDATE intent_journal SET state = 'ACCEPTED'"


# --- statement gate -------------------------------------------------------


class _Gates:
    """Pause a named thread immediately before a matching SQL statement."""

    def __init__(self):
        self._gates = {}

    def add(self, thread_name, pattern):
        gate = {
            "pattern": re.compile(pattern),
            "reached": threading.Event(),
            "release": threading.Event(),
            "fired": False,
        }
        self._gates[thread_name] = gate
        return gate

    def check(self, sql):
        gate = self._gates.get(threading.current_thread().name)
        if gate is None or gate["fired"]:
            return
        if gate["pattern"].search(" ".join(sql.split())):
            gate["fired"] = True
            gate["reached"].set()
            if not gate["release"].wait(TIMEOUT):
                raise RuntimeError("test gate was never released")

    def release_all(self):
        for gate in self._gates.values():
            gate["release"].set()


def _gated_factory(gates):
    class GatedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            gates.check(sql)
            return super().execute(sql, *args)

    return GatedConnection


class _SqliteShim:
    """Delegates to sqlite3; connect() uses a supplied connection factory."""

    def __init__(self, factory):
        self._factory = factory

    def connect(self, *args, **kwargs):
        kwargs.setdefault("factory", self._factory)
        return sqlite3.connect(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(sqlite3, name)


@pytest.fixture
def gates(monkeypatch):
    registry = _Gates()
    monkeypatch.setattr(adapter_module, "sqlite3", _SqliteShim(_gated_factory(registry)))
    try:
        yield registry
    finally:
        registry.release_all()


def _gated_ledger(path, gates, monkeypatch):
    ledger = CheckpointLedger(path)
    factory = _gated_factory(gates)
    monkeypatch.setattr(
        ledger, "_connect", lambda: sqlite3.connect(ledger.database_path, timeout=5.0, factory=factory)
    )
    return ledger


def _adapter(ledger, receipts):
    return OfflineAuthenticatedAdapter(ledger, receipts, trusted_fixture=True, halt_check=lambda: False)


# --- AT-0: joint consistency of both databases ----------------------------


def assert_joint_consistency(ledger_path, receipts_path, expect_ids=None):
    """Ledger events, ACCEPTED intents, accepted receipts and sequence state agree."""
    r = sqlite3.connect(receipts_path)
    lg = sqlite3.connect(ledger_path)
    try:
        intents = r.execute(
            "SELECT message_id, idempotency_key, project_id, task_id, checkpoint_id,"
            " assignment_id, payload_digest, sequence, state FROM intent_journal"
        ).fetchall()
        receipts = r.execute(
            "SELECT message_id, idempotency_key, project_id, task_id, checkpoint_id,"
            " assignment_id, sender FROM audit_receipts WHERE outcome = 'accepted'"
        ).fetchall()
        seq_state = dict(r.execute("SELECT stream_key, next_sequence FROM sequence_state").fetchall())
        stopped = r.execute("SELECT stopped FROM fail_stop WHERE singleton = 1").fetchone()
        events = lg.execute(
            "SELECT message_id, idempotency_key, project_id, task_id, checkpoint_id, actor, payload"
            " FROM events ORDER BY revision"
        ).fetchall()
    finally:
        r.close()
        lg.close()

    assert [i for i in intents if i[8] != "ACCEPTED"] == [], "unresolved or non-ACCEPTED intent remains"
    accepted = {i[0]: i for i in intents}
    receipt_rows = {}
    for row in receipts:
        assert row[0] not in receipt_rows, f"duplicate accepted receipt {row[0]}"
        receipt_rows[row[0]] = row
    adapter_events = {}
    for e in events:
        payload = json.loads(e[6]) if e[6] else {}
        if payload.get("adapter_origin") == "bridge-02a":
            adapter_events[e[0]] = (e, payload)

    assert set(adapter_events) == set(accepted) == set(receipt_rows)
    for msg_id, intent in accepted.items():
        event, payload = adapter_events[msg_id]
        receipt = receipt_rows[msg_id]
        assert intent[1:5] == event[1:5]                      # idempotency, project, task, checkpoint
        assert intent[5] == payload.get("assignment_id")
        assert intent[6] == payload.get("digest")
        assert receipt[1:6] == intent[1:6]
        assert receipt[6] == event[5]                         # sender == ledger actor

    streams = {}
    for intent in accepted.values():
        streams.setdefault(f"{intent[2]}:{intent[3]}", []).append(intent[7])
    assert set(streams) == set(seq_state)
    for stream, seqs in streams.items():
        assert sorted(seqs) == list(range(1, len(seqs) + 1))
        assert seq_state[stream] == len(seqs) + 1
    assert stopped == (0,)
    if expect_ids is not None:
        assert set(accepted) == set(expect_ids)
    # A fresh instance must reconcile cleanly.
    OfflineAuthenticatedAdapter(
        CheckpointLedger(ledger_path), receipts_path, trusted_fixture=True, halt_check=lambda: False
    )


def _assert_no_false_corruption(errors):
    for err in errors:
        for text in FALSE_CORRUPTION:
            assert text not in err, f"false corruption reported under concurrency: {err}"


def _reserved_lock_free(receipts):
    """True if another connection can take the receipts write lock immediately."""
    probe = sqlite3.connect(receipts, timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


def _thread(name, target):
    return threading.Thread(target=target, name=name, daemon=True)


def test_at0_sequential_accepts_are_jointly_consistent(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = _adapter(ledger, receipts)
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids=set())
    adapter.accept(_envelope(), now=_now())
    adapter.accept(
        _envelope(task_id="bridge-03", assignment_id="asgn-3", message_id="msg-3", idempotency_key="idem-3"),
        now=_now(),
    )
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1", "msg-3"})


# --- AT-1: case A under the contract schedule ------------------------------


def test_at1_reconcile_while_writer_at_pending_seam_is_unresolved(tmp_path):
    """T2 finishes reconciliation (Unresolved) before T1 leaves _after_pending_commit."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    a1 = _adapter(ledger, receipts)
    paused, release = threading.Event(), threading.Event()
    results, errors = [], []

    def hook():
        paused.set()
        if not release.wait(TIMEOUT):
            raise RuntimeError("seam never released")

    a1._after_pending_commit = hook

    def t1():
        try:
            a1.accept(_envelope(message_id="msg-t1", idempotency_key="idem-t1"), now=_now())
            results.append("msg-t1")
        except Exception as exc:
            errors.append(str(exc))

    th1 = _thread("T1", t1)
    try:
        th1.start()
        assert paused.wait(TIMEOUT)
        with pytest.raises(OfflineAdapterError, match="Unresolved"):
            _adapter(ledger, receipts)
    finally:
        release.set()
        th1.join(TIMEOUT)
    assert not th1.is_alive()
    assert results == ["msg-t1"] and errors == []
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-t1"})


# --- AT-2..AT-4: the three observed torn-read straddles --------------------


def _straddle(tmp_path, monkeypatch, gates, t2_pattern, t1_pause_at_seam):
    """
    T2 constructs an adapter (and then accepts, as the concurrent test does)
    and is paused before ``t2_pattern``. T1 then runs a full accept. T1 is
    observed for at most BLOCK_OBSERVE seconds, T2 is released, and both are
    joined with bounded timeouts.
    """
    ledger = _gated_ledger(tmp_path / "ledger.db", gates, monkeypatch)
    receipts = tmp_path / "receipts.db"
    a1 = _adapter(ledger, receipts)
    seam_paused, seam_release = threading.Event(), threading.Event()
    t1_done = threading.Event()
    results, errors = [], []

    if t1_pause_at_seam:
        def hook():
            seam_paused.set()
            if not seam_release.wait(TIMEOUT):
                raise RuntimeError("seam never released")
        a1._after_pending_commit = hook

    def t1():
        try:
            a1.accept(_envelope(message_id="msg-t1", idempotency_key="idem-t1"), now=_now())
            results.append("msg-t1")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            t1_done.set()

    def t2():
        try:
            a2 = _adapter(ledger, receipts)
            a2.accept(_envelope(message_id="msg-t2", idempotency_key="idem-t2"), now=_now())
            results.append("msg-t2")
        except Exception as exc:
            errors.append(str(exc))

    gate = gates.add("T2", t2_pattern)
    th1, th2 = _thread("T1", t1), _thread("T2", t2)
    t1_blocked = None
    try:
        if t1_pause_at_seam:
            th1.start()
            assert seam_paused.wait(TIMEOUT)
            th2.start()
            assert gate["reached"].wait(TIMEOUT)
            seam_release.set()
        else:
            th2.start()
            assert gate["reached"].wait(TIMEOUT)
            th1.start()
        t1_blocked = not t1_done.wait(BLOCK_OBSERVE)
    finally:
        gates.release_all()
        seam_release.set()
        th2.join(TIMEOUT)
        th1.join(TIMEOUT)
    assert not th1.is_alive() and not th2.is_alive()
    return results, errors, t1_blocked, ledger, receipts


def _assert_single_accept_allowed_error(results, errors, tmp_path, receipts):
    _assert_no_false_corruption(errors)
    assert len(results) == 1, (results, errors)
    assert len(errors) == 1, (results, errors)
    assert any(word in errors[0] for word in ALLOWED), errors
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids=set(results))


def test_at2_ledger_read_before_w2_and_journal_after_w3_cannot_tear(tmp_path, monkeypatch, gates):
    """Observed 'ACCEPTED intent without matching ledger event' straddle."""
    results, errors, t1_blocked, _, receipts = _straddle(
        tmp_path, monkeypatch, gates, SQL_UNRESOLVED, t1_pause_at_seam=True
    )
    _assert_single_accept_allowed_error(results, errors, tmp_path, receipts)
    assert t1_blocked, "T1's W3 completed while T2 held its reconciliation view open"
    assert results == ["msg-t1"] and "Unresolved" in errors[0]


def test_at3_receipts_query_after_w3_cannot_tear(tmp_path, monkeypatch, gates):
    """Observed 'Extra accepted receipt without journal intent' straddle."""
    results, errors, t1_blocked, _, receipts = _straddle(
        tmp_path, monkeypatch, gates, SQL_RECEIPTS, t1_pause_at_seam=False
    )
    _assert_single_accept_allowed_error(results, errors, tmp_path, receipts)
    assert t1_blocked, "T1's W1 ran while T2 held its reconciliation view open"


def test_at4_sequence_query_after_w3_cannot_tear(tmp_path, monkeypatch, gates):
    """Observed 'Orphan sequence state' straddle."""
    results, errors, t1_blocked, _, receipts = _straddle(
        tmp_path, monkeypatch, gates, SQL_SEQSTATE, t1_pause_at_seam=False
    )
    _assert_single_accept_allowed_error(results, errors, tmp_path, receipts)
    assert t1_blocked, "T1's W1 ran while T2 held its reconciliation view open"


# --- AT-5: reverse direction / writer exclusion during the ledger read -----


def test_at5_writer_excluded_while_ledger_view_is_read(tmp_path, monkeypatch, gates):
    """T2 paused at its ledger read: no adapter W1/W2 may run until T2 releases the lock."""
    results, errors, t1_blocked, _, receipts = _straddle(
        tmp_path, monkeypatch, gates, SQL_LEDGER_VIEW, t1_pause_at_seam=False
    )
    _assert_single_accept_allowed_error(results, errors, tmp_path, receipts)
    assert t1_blocked, "an adapter write ran while T2 was collecting its ledger view"


# --- AT-6: control, ledger written but W3 not started ----------------------


def test_at6_reconcile_between_w2_and_w3_is_unresolved(tmp_path, monkeypatch, gates):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    a1 = _adapter(ledger, receipts)
    gate = gates.add("T1", SQL_MARK_ACCEPTED)
    results, errors = [], []

    def t1():
        try:
            a1.accept(_envelope(message_id="msg-t1", idempotency_key="idem-t1"), now=_now())
            results.append("msg-t1")
        except Exception as exc:
            errors.append(str(exc))

    th1 = _thread("T1", t1)
    try:
        th1.start()
        assert gate["reached"].wait(TIMEOUT)
        with pytest.raises(OfflineAdapterError, match="Unresolved"):
            _adapter(ledger, receipts)
    finally:
        gates.release_all()
        th1.join(TIMEOUT)
    assert not th1.is_alive()
    assert results == ["msg-t1"] and errors == []
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-t1"})


# --- AT-7: real corruption is still detected, with unchanged messages ------


def _accepted_store(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    _adapter(ledger, receipts).accept(_envelope(), now=_now())
    return ledger, receipts


def _raw(path, sql, params=()):
    c = sqlite3.connect(path)
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def test_at7a_missing_ledger_event_detected(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(tmp_path / "ledger.db", "DELETE FROM events WHERE message_id = 'msg-1'")
    with pytest.raises(OfflineAdapterError, match="^ACCEPTED intent without matching ledger event: msg-1$"):
        _adapter(ledger, receipts)


def test_at7b_extra_receipt_detected(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(receipts,
         "INSERT INTO audit_receipts (message_id, idempotency_key, project_id, task_id, checkpoint_id,"
         " assignment_id, sender, outcome, reason, recorded_at)"
         " VALUES ('ghost', 'ghost-idem', 'QLA', 'bridge-02', 'cp-1', 'asgn-1', 'chief', 'accepted', NULL, 'x')")
    with pytest.raises(OfflineAdapterError, match="^Extra accepted receipt without journal intent: ghost$"):
        _adapter(ledger, receipts)


def test_at7c_orphan_sequence_detected(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(receipts, "INSERT INTO sequence_state (stream_key, next_sequence) VALUES ('QLA:other', 5)")
    with pytest.raises(OfflineAdapterError, match="^Orphan sequence state for QLA:other$"):
        _adapter(ledger, receipts)


def test_at7d_adapter_ledger_event_without_intent_detected(tmp_path):
    ledger, _ = _accepted_store(tmp_path)
    with pytest.raises(OfflineAdapterError, match="^Adapter ledger event without ACCEPTED intent: msg-1$"):
        _adapter(ledger, tmp_path / "other-receipts.db")


def test_at7e_sequence_state_mismatch_detected(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(receipts, "UPDATE sequence_state SET next_sequence = 7")
    with pytest.raises(OfflineAdapterError, match="^Sequence state mismatch or missing for QLA:bridge-02$"):
        _adapter(ledger, receipts)


def test_at7f_pending_intent_detected(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(receipts, "UPDATE intent_journal SET state = 'PENDING'")
    with pytest.raises(OfflineAdapterError, match=r"^Unresolved intent requires manual reconciliation: \['msg-1'\]$"):
        _adapter(ledger, receipts)


# --- AT-8: PENDING is durable before the ledger write ----------------------


def test_at8_pending_committed_before_ledger_write(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    a1 = _adapter(ledger, receipts)
    paused, release = threading.Event(), threading.Event()
    seen = {}

    def hook():
        paused.set()
        if not release.wait(TIMEOUT):
            raise RuntimeError("seam never released")

    a1._after_pending_commit = hook
    th1 = _thread("T1", lambda: a1.accept(_envelope(), now=_now()))
    try:
        th1.start()
        assert paused.wait(TIMEOUT)
        r = sqlite3.connect(receipts)
        lg = sqlite3.connect(tmp_path / "ledger.db")
        try:
            seen["journal"] = r.execute("SELECT message_id, state FROM intent_journal").fetchall()
            seen["events"] = lg.execute("SELECT message_id FROM events").fetchall()
        finally:
            r.close()
            lg.close()
        seen["lock_free"] = _reserved_lock_free(receipts)
    finally:
        release.set()
        th1.join(TIMEOUT)
    assert not th1.is_alive()
    assert seen["journal"] == [("msg-1", "PENDING")]
    assert seen["events"] == []
    assert seen["lock_free"], "no receipts transaction may be held across PENDING -> ledger"
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1"})


# --- AT-9: accept() reconciles inside its own admission transaction --------


def test_at9_accept_reconcile_runs_under_admission_lock(tmp_path, gates):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = _adapter(ledger, receipts)
    gate = gates.add("T2", SQL_UNRESOLVED)
    results, errors = [], []
    observed = {}

    def t2():
        try:
            adapter.accept(_envelope(), now=_now())
            results.append("msg-1")
        except Exception as exc:
            errors.append(str(exc))

    th2 = _thread("T2", t2)
    try:
        th2.start()
        assert gate["reached"].wait(TIMEOUT)
        observed["lock_free"] = _reserved_lock_free(receipts)
    finally:
        gates.release_all()
        th2.join(TIMEOUT)
    assert not th2.is_alive()
    assert observed["lock_free"] is False, "accept() reconciliation ran outside the admission lock"
    assert results == ["msg-1"] and errors == []
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1"})


# --- AT-10a: repeated unforced two-instance race (in-suite sample) ---------


def test_at10a_repeated_two_instance_race_is_consistent(tmp_path):
    for i in range(50):
        base = tmp_path / f"r{i}"
        base.mkdir()
        ledger = CheckpointLedger(base / "ledger.db")
        receipts = base / "receipts.db"
        results, errors = [], []

        def attempt(msg_id, idem):
            try:
                _adapter(ledger, receipts).accept(
                    _envelope(message_id=msg_id, idempotency_key=idem), now=_now()
                )
                results.append(msg_id)
            except Exception as exc:
                errors.append(str(exc))

        threads = [_thread("A", lambda: attempt("msg-t1", "idem-t1")),
                   _thread("B", lambda: attempt("msg-t2", "idem-t2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(TIMEOUT)
        assert not any(t.is_alive() for t in threads)
        _assert_no_false_corruption(errors)
        assert len(results) == 1 and len(errors) == 1, (i, results, errors)
        assert any(word in errors[0] for word in ALLOWED), (i, errors)
        assert_joint_consistency(base / "ledger.db", receipts, expect_ids=set(results))


# --- AT-12: constructor-path ownership when BEGIN IMMEDIATE or a read fails -


class _OwnershipRecorder:
    def __init__(self):
        self.connections = []
        self.fail_begin = False
        self.fail_on = None

    def factory(self):
        recorder = self

        class RecordingConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.log = []
                self.closed_in_transaction = None
                recorder.connections.append(self)

            def execute(self, sql, *args):
                flat = " ".join(sql.split())
                self.log.append(flat[:40])
                if recorder.fail_begin and flat.upper().startswith("BEGIN IMMEDIATE"):
                    raise sqlite3.OperationalError("injected BEGIN IMMEDIATE failure")
                if recorder.fail_on and re.search(recorder.fail_on, flat):
                    raise sqlite3.OperationalError("injected receipts read failure")
                return super().execute(sql, *args)

            def commit(self):
                self.log.append("commit()")
                return super().commit()

            def rollback(self):
                self.log.append("rollback()")
                return super().rollback()

            def close(self):
                if self.closed_in_transaction is None:
                    self.closed_in_transaction = self.in_transaction
                self.log.append("close()")
                return super().close()

        return RecordingConnection


def _is_closed(connection):
    try:
        connection.total_changes
    except sqlite3.ProgrammingError:
        return True
    return False


@pytest.fixture
def recorder(monkeypatch):
    rec = _OwnershipRecorder()
    monkeypatch.setattr(adapter_module, "sqlite3", _SqliteShim(rec.factory()))
    return rec


def test_at12a_begin_immediate_failure_closes_without_transaction(tmp_path, recorder):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = _adapter(ledger, receipts)
    recorder.connections.clear()
    recorder.fail_begin = True
    with pytest.raises(OfflineAdapterError, match="Intent journal unreadable: injected BEGIN IMMEDIATE failure"):
        adapter._reconcile()
    assert len(recorder.connections) == 1
    conn = recorder.connections[0]
    assert _is_closed(conn)
    assert conn.closed_in_transaction is False
    assert "rollback()" not in conn.log           # no transaction existed
    assert _reserved_lock_free(receipts)


def test_at12b_read_failure_after_begin_rolls_back_and_closes(tmp_path, recorder):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = _adapter(ledger, receipts)
    recorder.connections.clear()
    recorder.fail_on = SQL_SEQSTATE
    with pytest.raises(OfflineAdapterError, match="Intent journal unreadable: injected receipts read failure"):
        adapter._reconcile()
    assert len(recorder.connections) == 1
    conn = recorder.connections[0]
    assert conn.log[0].startswith("BEGIN IMMEDIATE")
    assert "rollback()" in conn.log
    assert conn.log[-1] == "close()"
    assert conn.closed_in_transaction is False
    assert _reserved_lock_free(receipts)


def test_at12c_success_rolls_back_writes_nothing_and_closes(tmp_path, recorder):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = _adapter(ledger, receipts)
    recorder.connections.clear()
    adapter._reconcile()
    conn = recorder.connections[0]
    assert conn.log[0].startswith("BEGIN IMMEDIATE")
    assert "commit()" not in conn.log
    assert conn.log[-2:] == ["rollback()", "close()"]
    assert conn.closed_in_transaction is False
    assert _reserved_lock_free(receipts)


def test_at12d_real_lock_contention_fails_closed_and_closes(tmp_path, recorder):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = _adapter(ledger, receipts)
    holder = sqlite3.connect(receipts)
    try:
        holder.execute("BEGIN IMMEDIATE")
        recorder.connections.clear()
        with pytest.raises(OfflineAdapterError, match="Intent journal unreadable: database is locked"):
            adapter._reconcile()
    finally:
        holder.rollback()
        holder.close()
    conn = recorder.connections[0]
    assert _is_closed(conn) and conn.closed_in_transaction is False
    assert "rollback()" not in conn.log


# --- AT-13: accept-path borrowed connection is never committed/rolled back/closed


def _borrowed(receipts, recorder):
    conn = sqlite3.connect(receipts, factory=recorder.factory())
    conn.execute("BEGIN IMMEDIATE")
    conn.log.clear()
    return conn


def test_at13a_borrowed_connection_untouched_on_success(tmp_path, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    conn = _borrowed(receipts, recorder)
    try:
        adapter._reconcile(conn)
        assert not _is_closed(conn)
        assert conn.in_transaction
        assert not any(e.startswith(("BEGIN", "COMMIT", "ROLLBACK")) or e.endswith("()") for e in conn.log), conn.log
        assert not _reserved_lock_free(receipts)          # caller's lock still held
    finally:
        conn.rollback()
        conn.close()


def test_at13b_borrowed_connection_untouched_on_failure(tmp_path, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    conn = _borrowed(receipts, recorder)
    try:
        conn.execute("INSERT INTO sequence_state (stream_key, next_sequence) VALUES ('QLA:other', 5)")
        conn.log.clear()
        with pytest.raises(OfflineAdapterError, match="^Orphan sequence state for QLA:other$"):
            adapter._reconcile(conn)
        assert not _is_closed(conn)
        assert conn.in_transaction
        assert not any(e.startswith(("BEGIN", "COMMIT", "ROLLBACK")) or e.endswith("()") for e in conn.log), conn.log
    finally:
        conn.rollback()
        conn.close()
    # The caller's uncommitted row was rolled back by the caller, not by _reconcile.
    _adapter(ledger, receipts)


def test_at13c_borrowed_connection_without_transaction_is_refused(tmp_path, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    conn = sqlite3.connect(receipts, factory=recorder.factory())
    try:
        with pytest.raises(OfflineAdapterError, match="caller's open receipts transaction"):
            adapter._reconcile(conn)
        assert not conn.in_transaction
        assert conn.log == []
    finally:
        conn.close()


# --- AT-14: ledger connections are closed on both paths --------------------


def _track_ledger(monkeypatch, ledger):
    """Record ledger._connect() results only when the adapter module is the caller."""
    opened = []
    real = ledger._connect

    def tracked():
        connection = real()
        if sys._getframe(1).f_globals.get("__name__") == adapter_module.__name__:
            opened.append(connection)
        return connection

    monkeypatch.setattr(ledger, "_connect", tracked)
    return opened


def test_at14a_ledger_connections_closed_constructor_and_accept(tmp_path, monkeypatch):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    opened = _track_ledger(monkeypatch, ledger)
    adapter = _adapter(ledger, tmp_path / "receipts.db")
    adapter.accept(_envelope(), now=_now())
    assert opened
    assert all(_is_closed(c) for c in opened)


def test_at14b_ledger_connection_closed_when_ledger_view_fails(tmp_path, monkeypatch):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(tmp_path / "ledger.db", "UPDATE events SET payload = '{not json'")
    opened = _track_ledger(monkeypatch, ledger)
    with pytest.raises(OfflineAdapterError, match="Ledger unreadable during reconciliation: Malformed ledger payload"):
        _adapter(ledger, receipts)
    assert opened and all(_is_closed(c) for c in opened)
    assert _reserved_lock_free(receipts)


# --- AT-15: accept() owns cleanup when its reconciliation fails ------------


def test_at15_accept_rolls_back_and_closes_when_reconcile_fails(tmp_path, monkeypatch, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    _raw(receipts, "INSERT INTO sequence_state (stream_key, next_sequence) VALUES ('QLA:other', 5)")
    recorder.connections.clear()
    with pytest.raises(OfflineAdapterError, match="^Orphan sequence state for QLA:other$"):
        adapter.accept(_envelope(message_id="msg-2", idempotency_key="idem-2", sequence=2), now=_now())
    assert recorder.connections, "accept() opened no receipts connection"
    assert all(_is_closed(c) for c in recorder.connections)
    assert all(c.closed_in_transaction is False for c in recorder.connections)
    assert _reserved_lock_free(receipts)
    c = sqlite3.connect(receipts)
    try:
        assert c.execute("SELECT message_id FROM intent_journal ORDER BY intent_id").fetchall() == [("msg-1",)]
        assert c.execute("SELECT message_id FROM audit_receipts ORDER BY receipt_id").fetchall() == [("msg-1",)]
        assert c.execute("SELECT stopped FROM fail_stop").fetchone() == (0,)
    finally:
        c.close()
    assert adapter.execution_enabled is False


# ===========================================================================
# Remaining acceptance coverage (orig AT-6, AT-8, AT-12/13/14, AT-15)
# ===========================================================================


def _snapshot(path):
    """Full logical content of one database (schema and rows)."""
    c = sqlite3.connect(path)
    try:
        return list(c.iterdump())
    finally:
        c.close()


def assert_fresh_writer_progress(*paths):
    """
    A fresh independent writer takes BEGIN IMMEDIATE without waiting, runs a
    harmless synthetic transaction, and rolls it back; nothing persists.
    """
    for path in paths:
        before = _snapshot(path)
        writer = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            assert writer.in_transaction
            writer.execute("CREATE TABLE writer_progress_probe (x INTEGER NOT NULL)")
            writer.execute("INSERT INTO writer_progress_probe (x) VALUES (1)")
            assert writer.execute("SELECT count(*) FROM writer_progress_probe").fetchone() == (1,)
            writer.execute("ROLLBACK")
            assert not writer.in_transaction
        finally:
            writer.close()
        assert _snapshot(path) == before, f"writer-progress probe left a change in {path}"


_FORBIDDEN_ON_BORROWED = ("BEGIN", "COMMIT", "ROLLBACK", "END")


def _spy_reconcile(adapter):
    """Record how accept() calls _reconcile and what _reconcile does to its connection."""
    record = {"calls": []}
    real = adapter._reconcile

    def spy(connection=None):
        call = {
            "connection": connection,
            "in_tx_before": None if connection is None else connection.in_transaction,
        }
        start = len(connection.log) if connection is not None and hasattr(connection, "log") else None
        record["calls"].append(call)
        try:
            return real() if connection is None else real(connection)
        except BaseException as exc:
            call["error"] = str(exc)
            raise
        finally:
            if connection is not None:
                call["closed_after"] = _is_closed(connection)
                call["in_tx_after"] = None if call["closed_after"] else connection.in_transaction
                call["log_during"] = None if start is None else list(connection.log[start:])

    adapter._reconcile = spy
    return record


def _assert_borrowed_untouched(record):
    """accept() reconciled on its own open transaction; _reconcile left it alone."""
    assert len(record["calls"]) == 1, record
    call = record["calls"][0]
    assert call["connection"] is not None, "accept() reconciled outside its admission transaction"
    assert call["in_tx_before"] is True
    assert call["closed_after"] is False, "_reconcile closed the borrowed connection"
    assert call["in_tx_after"] is True, "_reconcile ended the borrowed transaction"
    assert not any(
        e.upper().startswith(_FORBIDDEN_ON_BORROWED) or e.endswith("()") for e in call["log_during"]
    ), call["log_during"]
    return call


def _assert_accept_owned_cleanup(recorder):
    """
    accept() ended its own transaction without committing and closed its own
    connection. _reconcile left the transaction open (_assert_borrowed_untouched),
    so closed_in_transaction is False proves accept()'s connection context
    rolled it back before closing() closed it. (The context manager's rollback
    runs in C and is not visible as a Python-level rollback() call.)
    """
    assert len(recorder.connections) == 1, "accept() should open exactly one receipts connection"
    conn = recorder.connections[0]
    assert _is_closed(conn)
    assert conn.closed_in_transaction is False
    assert "commit()" not in conn.log
    assert conn.log[-1] == "close()"
    return conn


def _assert_owned_cleanup(recorder, began=True):
    """Constructor-path _reconcile rolled back only if a transaction existed, and closed."""
    assert len(recorder.connections) == 1
    conn = recorder.connections[0]
    assert _is_closed(conn)
    assert conn.closed_in_transaction is False
    assert "commit()" not in conn.log
    assert conn.log[-1] == "close()"
    if began:
        assert conn.log[0].startswith("BEGIN IMMEDIATE")
        assert "rollback()" in conn.log
    else:
        assert "rollback()" not in conn.log
    return conn


class _LedgerViewFault:
    """Injects a failure into the adapter's ledger-view query while armed."""

    def __init__(self, ledger, monkeypatch, exc_factory):
        self.armed = True
        self.opened = []
        fault = self

        class FaultyLedgerConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if fault.armed and re.search(SQL_LEDGER_VIEW, " ".join(sql.split())):
                    raise exc_factory()
                return super().execute(sql, *args)

        def connect():
            connection = sqlite3.connect(ledger.database_path, timeout=5.0, factory=FaultyLedgerConnection)
            if sys._getframe(1).f_globals.get("__name__") == adapter_module.__name__:
                fault.opened.append(connection)
            return connection

        monkeypatch.setattr(ledger, "_connect", connect)


def _hold_lock(path, mode):
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute(f"BEGIN {mode}")
    return holder


def _release_lock(holder):
    try:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
    finally:
        holder.close()


def _second_stream_envelope():
    return _envelope(task_id="bridge-03", assignment_id="asgn-3", message_id="msg-3", idempotency_key="idem-3")


# --- orig AT-12/13/14: actual lock contention ------------------------------


def test_orig_at12_14_constructor_receipts_lock_contention_then_writer_progress(tmp_path, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    holder = _hold_lock(receipts, "IMMEDIATE")
    try:
        recorder.connections.clear()
        with pytest.raises(OfflineAdapterError, match="^Intent journal unreadable: database is locked$"):
            adapter._reconcile()
        _assert_owned_cleanup(recorder, began=False)
    finally:
        _release_lock(holder)
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1"})


def test_orig_at12_14_accept_receipts_lock_contention_then_writer_progress(tmp_path, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    record = _spy_reconcile(adapter)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    holder = _hold_lock(receipts, "IMMEDIATE")
    try:
        recorder.connections.clear()
        with pytest.raises(sqlite3.OperationalError, match="^database is locked$"):
            adapter.accept(_second_stream_envelope(), now=_now())
        # accept() never reached reconciliation: no reconcile outside its lock.
        assert record["calls"] == [], "accept() reconciled before obtaining its admission lock"
        conn = _assert_accept_owned_cleanup(recorder)
        assert conn.log[0].startswith("BEGIN IMMEDIATE")
    finally:
        _release_lock(holder)
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    assert adapter.accept(_second_stream_envelope(), now=_now()) == "accepted"
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1", "msg-3"})


def test_orig_at12_14_constructor_ledger_lock_contention_then_writer_progress(tmp_path, monkeypatch, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    opened = _track_ledger(monkeypatch, ledger)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    holder = _hold_lock(tmp_path / "ledger.db", "EXCLUSIVE")
    try:
        recorder.connections.clear()
        with pytest.raises(OfflineAdapterError, match="^Ledger unreadable during reconciliation: database is locked$"):
            adapter._reconcile()
        _assert_owned_cleanup(recorder, began=True)
        assert opened and all(_is_closed(c) for c in opened)
    finally:
        _release_lock(holder)
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1"})


def test_orig_at12_14_accept_ledger_lock_contention_then_writer_progress(tmp_path, monkeypatch, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    record = _spy_reconcile(adapter)
    opened = _track_ledger(monkeypatch, ledger)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    holder = _hold_lock(tmp_path / "ledger.db", "EXCLUSIVE")
    try:
        recorder.connections.clear()
        with pytest.raises(OfflineAdapterError, match="^Ledger unreadable during reconciliation: database is locked$"):
            adapter.accept(_second_stream_envelope(), now=_now())
        _assert_borrowed_untouched(record)
        _assert_accept_owned_cleanup(recorder)
        assert opened and all(_is_closed(c) for c in opened)
    finally:
        _release_lock(holder)
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    assert adapter.accept(_second_stream_envelope(), now=_now()) == "accepted"
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1", "msg-3"})


# --- orig AT-12/13/14: ledger-view failure ---------------------------------


@pytest.mark.parametrize("exc_factory", [
    lambda: sqlite3.OperationalError("injected ledger view failure"),
    lambda: RuntimeError("injected ledger view failure"),
], ids=["sqlite-error", "runtime-error"])
def test_orig_at12_14_constructor_ledger_view_failure_then_writer_progress(tmp_path, monkeypatch, recorder, exc_factory):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    fault = _LedgerViewFault(ledger, monkeypatch, exc_factory)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    recorder.connections.clear()
    with pytest.raises(OfflineAdapterError, match="^Ledger unreadable during reconciliation: injected ledger view failure$"):
        adapter._reconcile()
    _assert_owned_cleanup(recorder, began=True)
    assert fault.opened and all(_is_closed(c) for c in fault.opened)
    assert _reserved_lock_free(receipts)
    fault.armed = False
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    adapter._reconcile()
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1"})


@pytest.mark.parametrize("exc_factory", [
    lambda: sqlite3.OperationalError("injected ledger view failure"),
    lambda: RuntimeError("injected ledger view failure"),
], ids=["sqlite-error", "runtime-error"])
def test_orig_at12_14_accept_ledger_view_failure_then_writer_progress(tmp_path, monkeypatch, recorder, exc_factory):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    record = _spy_reconcile(adapter)
    fault = _LedgerViewFault(ledger, monkeypatch, exc_factory)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    recorder.connections.clear()
    with pytest.raises(OfflineAdapterError, match="^Ledger unreadable during reconciliation: injected ledger view failure$"):
        adapter.accept(_second_stream_envelope(), now=_now())
    _assert_borrowed_untouched(record)
    _assert_accept_owned_cleanup(recorder)
    assert fault.opened and all(_is_closed(c) for c in fault.opened)
    assert _reserved_lock_free(receipts)
    fault.armed = False
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    assert adapter.accept(_second_stream_envelope(), now=_now()) == "accepted"
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1", "msg-3"})


# --- orig AT-12/13/14: mid-reconciliation exception ------------------------


@pytest.mark.parametrize("pattern", [SQL_RECEIPTS, SQL_SEQSTATE], ids=["third-query", "fourth-query"])
def test_orig_at12_14_constructor_mid_reconcile_exception_then_writer_progress(tmp_path, recorder, pattern):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    recorder.connections.clear()
    recorder.fail_on = pattern
    try:
        with pytest.raises(OfflineAdapterError, match="^Intent journal unreadable: injected receipts read failure$"):
            adapter._reconcile()
        _assert_owned_cleanup(recorder, began=True)
    finally:
        recorder.fail_on = None
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    adapter._reconcile()
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1"})


@pytest.mark.parametrize("pattern", [SQL_RECEIPTS, SQL_SEQSTATE], ids=["third-query", "fourth-query"])
def test_orig_at12_14_accept_mid_reconcile_exception_then_writer_progress(tmp_path, recorder, pattern):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    record = _spy_reconcile(adapter)
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    recorder.connections.clear()
    recorder.fail_on = pattern
    try:
        with pytest.raises(OfflineAdapterError, match="^Intent journal unreadable: injected receipts read failure$"):
            adapter.accept(_second_stream_envelope(), now=_now())
        _assert_borrowed_untouched(record)
        _assert_accept_owned_cleanup(recorder)
    finally:
        recorder.fail_on = None
    assert _reserved_lock_free(receipts)
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    assert adapter.accept(_second_stream_envelope(), now=_now()) == "accepted"
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-1", "msg-3"})


def test_orig_at12_14_constructor_corruption_fixture_remains_writer_still_progresses(tmp_path, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    _raw(receipts, "INSERT INTO sequence_state (stream_key, next_sequence) VALUES ('QLA:other', 5)")
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    recorder.connections.clear()
    with pytest.raises(OfflineAdapterError, match="^Orphan sequence state for QLA:other$"):
        adapter._reconcile()
    _assert_owned_cleanup(recorder, began=True)
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    # The fixture stays: a raw writer progresses, the adapter still refuses.
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    with pytest.raises(OfflineAdapterError, match="^Orphan sequence state for QLA:other$"):
        _adapter(ledger, receipts)


def test_orig_at12_14_accept_corruption_fixture_remains_writer_still_progresses(tmp_path, recorder):
    ledger, receipts = _accepted_store(tmp_path)
    adapter = _adapter(ledger, receipts)
    record = _spy_reconcile(adapter)
    _raw(receipts, "INSERT INTO sequence_state (stream_key, next_sequence) VALUES ('QLA:other', 5)")
    before = (_snapshot(receipts), _snapshot(tmp_path / "ledger.db"))
    recorder.connections.clear()
    with pytest.raises(OfflineAdapterError, match="^Orphan sequence state for QLA:other$"):
        adapter.accept(_second_stream_envelope(), now=_now())
    _assert_borrowed_untouched(record)
    _assert_accept_owned_cleanup(recorder)
    assert (_snapshot(receipts), _snapshot(tmp_path / "ledger.db")) == before
    assert_fresh_writer_progress(receipts, tmp_path / "ledger.db")
    with pytest.raises(OfflineAdapterError, match="^Orphan sequence state for QLA:other$"):
        adapter.accept(_second_stream_envelope(), now=_now())


# --- orig AT-8: accept() decides on its own lock after an S0 constructor ---


def test_orig_at8_accept_after_s0_constructor_sees_inflight_pending(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    a1 = _adapter(ledger, receipts)
    a2 = _adapter(ledger, receipts)          # constructor reconciled at S0
    paused, release = threading.Event(), threading.Event()
    results, errors = [], []

    def hook():
        paused.set()
        if not release.wait(TIMEOUT):
            raise RuntimeError("seam never released")

    a1._after_pending_commit = hook

    def t1():
        try:
            a1.accept(_envelope(message_id="msg-t1", idempotency_key="idem-t1"), now=_now())
            results.append("msg-t1")
        except Exception as exc:
            errors.append(str(exc))

    th1 = _thread("T1", t1)
    seen = {}
    try:
        th1.start()
        assert paused.wait(TIMEOUT)
        try:
            a2.accept(_envelope(message_id="msg-t2", idempotency_key="idem-t2"), now=_now())
            seen["t2"] = "accepted"
        except OfflineAdapterError as exc:
            seen["t2"] = str(exc)
    finally:
        release.set()
        th1.join(TIMEOUT)
    assert not th1.is_alive()
    _assert_no_false_corruption([seen["t2"]])
    assert seen["t2"].startswith(("Unresolved intent", "Active unresolved intent")), seen
    assert results == ["msg-t1"] and errors == []
    assert_joint_consistency(tmp_path / "ledger.db", receipts, expect_ids={"msg-t1"})


# --- orig AT-6: remaining reconciliation messages, exact and unchanged -----


def _raw_script(path, script):
    c = sqlite3.connect(path)
    try:
        c.executescript(script)
    finally:
        c.close()


def test_orig_at6_identity_mismatch_exact_message(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(receipts, "UPDATE intent_journal SET checkpoint_id = 'cp-x'")
    with pytest.raises(OfflineAdapterError, match="^Identity mismatch for msg-1$"):
        _adapter(ledger, receipts)


def test_orig_at6_missing_accepted_receipt_exact_message(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw_script(receipts, "DROP TRIGGER audit_receipts_no_delete; DELETE FROM audit_receipts;")
    with pytest.raises(OfflineAdapterError, match="^Missing accepted receipt for msg-1$"):
        _adapter(ledger, receipts)


def test_orig_at6_receipt_identity_mismatch_exact_message(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw_script(receipts, "DROP TRIGGER audit_receipts_no_update; UPDATE audit_receipts SET checkpoint_id = 'cp-x';")
    with pytest.raises(OfflineAdapterError, match="^Receipt identity mismatch for msg-1$"):
        _adapter(ledger, receipts)


def test_orig_at6_sender_mismatch_exact_message(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw_script(receipts, "DROP TRIGGER audit_receipts_no_update; UPDATE audit_receipts SET sender = 'mallory';")
    with pytest.raises(OfflineAdapterError, match="^Sender mismatch for msg-1$"):
        _adapter(ledger, receipts)


def test_orig_at6_duplicate_accepted_receipts_exact_message(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw(receipts,
         "INSERT INTO audit_receipts (message_id, idempotency_key, project_id, task_id, checkpoint_id,"
         " assignment_id, sender, outcome, reason, recorded_at)"
         " VALUES ('msg-1', 'idem-1', 'QLA', 'bridge-02', 'cp-1', 'asgn-1', 'chief', 'accepted', NULL, 'x')")
    # Existing behaviour, unchanged: raised inside the receipts read block and
    # therefore reported through the existing 'Intent journal unreadable' wrapper.
    with pytest.raises(OfflineAdapterError, match=r"^Intent journal unreadable: Duplicate accepted receipts: \['msg-1'\]$"):
        _adapter(ledger, receipts)


def test_orig_at6_sequence_not_starting_at_one_exact_message(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _raw_script(receipts, "UPDATE intent_journal SET sequence = 2; UPDATE sequence_state SET next_sequence = 3;")
    with pytest.raises(OfflineAdapterError, match="^Sequence does not start at 1 for QLA:bridge-02$"):
        _adapter(ledger, receipts)


def test_orig_at6_sequence_gap_exact_message(tmp_path):
    ledger, receipts = _accepted_store(tmp_path)
    _adapter(ledger, receipts).accept(_second_stream_envelope(), now=_now())
    # Re-home msg-3 into stream QLA:bridge-02 at sequence 3 consistently in all
    # three stores, leaving a gap at sequence 2.
    _raw(tmp_path / "ledger.db", "UPDATE events SET task_id = 'bridge-02' WHERE message_id = 'msg-3'")
    _raw_script(receipts, (
        "DROP TRIGGER audit_receipts_no_update;"
        "UPDATE audit_receipts SET task_id = 'bridge-02' WHERE message_id = 'msg-3';"
        "UPDATE intent_journal SET task_id = 'bridge-02', sequence = 3 WHERE message_id = 'msg-3';"
        "DELETE FROM sequence_state WHERE stream_key = 'QLA:bridge-03';"
        "UPDATE sequence_state SET next_sequence = 4 WHERE stream_key = 'QLA:bridge-02';"
    ))
    with pytest.raises(OfflineAdapterError, match="^Sequence gap in stream QLA:bridge-02$"):
        _adapter(ledger, receipts)


# --- orig AT-15: separate-process SQLite exclusion (spawn, Windows-safe) ---
#
# Children are module-level functions started with the "spawn" context, so
# each child imports this module afresh, builds its own CheckpointLedger and
# OfflineAuthenticatedAdapter and shares no Python lock with the parent or the
# other child. Exclusion can only come from the SQLite file lock.

STARTUP_TIMEOUT = 120.0   # spawn + import on Windows; bounded
CHILD_GATE_TIMEOUT = 60.0


def _child_write(out_path, payload):
    payload["pid"] = os.getpid()
    Path(out_path).write_text(json.dumps(payload), encoding="utf-8")


def _at15_reconciler_child(ledger_path, receipts_path, mode, holding, release, done, out_path):
    """Process 2: reconcile and pause inside the ledger view while holding the receipts lock."""
    try:
        import skippy.collaboration.offline_authenticated_adapter as child_adapter_module
        from skippy.collaboration.checkpoint_ledger import CheckpointLedger as ChildLedger

        state = {"armed": False, "fired": False, "lock_free_at_pause": None}

        class PausingLedgerConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if state["armed"] and not state["fired"] and re.search(SQL_LEDGER_VIEW, " ".join(sql.split())):
                    state["fired"] = True
                    state["lock_free_at_pause"] = _reserved_lock_free(receipts_path)
                    holding.set()
                    if not release.wait(CHILD_GATE_TIMEOUT):
                        raise RuntimeError("reconciler child gate was never released")
                return super().execute(sql, *args)

        ledger = ChildLedger(ledger_path)
        ledger._connect = lambda: sqlite3.connect(ledger.database_path, timeout=5.0, factory=PausingLedgerConnection)
        result = {"mode": mode}
        if mode == "constructor":
            state["armed"] = True
            try:
                child_adapter_module.OfflineAuthenticatedAdapter(
                    ledger, receipts_path, trusted_fixture=True, halt_check=lambda: False
                )
                result["outcome"] = "reconciled"
            except Exception as exc:
                result["outcome"] = "error"
                result["error"] = f"{type(exc).__name__}: {exc}"
        else:
            adapter = child_adapter_module.OfflineAuthenticatedAdapter(
                ledger, receipts_path, trusted_fixture=True, halt_check=lambda: False
            )
            state["armed"] = True
            try:
                result["outcome"] = adapter.accept(
                    _envelope(message_id="msg-p2", idempotency_key="idem-p2"), now=_now()
                )
            except Exception as exc:
                result["outcome"] = "error"
                result["error"] = f"{type(exc).__name__}: {exc}"
        result["fired"] = state["fired"]
        result["lock_free_at_pause"] = state["lock_free_at_pause"]
        _child_write(out_path, result)
    except BaseException as exc:
        _child_write(out_path, {"mode": mode, "outcome": "crash", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        holding.set()
        done.set()


def _at15_writer_child(ledger_path, receipts_path, envelope_kwargs, ready, go, attempting, locked, done, out_path):
    """Process 1: an independent adapter that attempts a conflicting admission."""
    try:
        import skippy.collaboration.offline_authenticated_adapter as child_adapter_module
        from skippy.collaboration.checkpoint_ledger import CheckpointLedger as ChildLedger

        adapter = child_adapter_module.OfflineAuthenticatedAdapter(
            ChildLedger(ledger_path), receipts_path, trusted_fixture=True, halt_check=lambda: False
        )

        class LockSignallingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                cursor = super().execute(sql, *args)
                if " ".join(sql.split()).upper().startswith("BEGIN IMMEDIATE"):
                    locked.set()
                return cursor

        child_adapter_module.sqlite3 = _SqliteShim(LockSignallingConnection)
        ready.set()
        if not go.wait(CHILD_GATE_TIMEOUT):
            raise RuntimeError("writer child was never told to go")
        attempting.set()
        try:
            result = {"outcome": adapter.accept(_envelope(**envelope_kwargs), now=_now())}
        except Exception as exc:
            result = {"outcome": "error", "error": f"{type(exc).__name__}: {exc}"}
        _child_write(out_path, result)
    except BaseException as exc:
        _child_write(out_path, {"outcome": "crash", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        ready.set()
        done.set()


def _read_child(out_path):
    path = Path(out_path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _run_two_process_exclusion(tmp_path, mode, writer_kwargs):
    ctx = multiprocessing.get_context("spawn")
    ledger_path = str(tmp_path / "ledger.db")
    receipts_path = str(tmp_path / "receipts.db")
    r_out, w_out = str(tmp_path / "p2-reconciler.json"), str(tmp_path / "p1-writer.json")
    ev = {name: ctx.Event() for name in (
        "holding", "release", "r_done", "ready", "go", "attempting", "locked", "w_done")}
    writer = ctx.Process(
        target=_at15_writer_child, name="p1-writer", daemon=True,
        args=(ledger_path, receipts_path, writer_kwargs, ev["ready"], ev["go"], ev["attempting"],
              ev["locked"], ev["w_done"], w_out),
    )
    reconciler = ctx.Process(
        target=_at15_reconciler_child, name="p2-reconciler", daemon=True,
        args=(ledger_path, receipts_path, mode, ev["holding"], ev["release"], ev["r_done"], r_out),
    )
    obs = {}
    try:
        writer.start()
        obs["writer_ready"] = ev["ready"].wait(STARTUP_TIMEOUT) and not ev["w_done"].is_set()
        assert obs["writer_ready"], ("writer child failed before the attempt", _read_child(w_out))
        reconciler.start()
        obs["reconciler_holding"] = ev["holding"].wait(STARTUP_TIMEOUT) and not ev["r_done"].is_set()
        assert obs["reconciler_holding"], ("reconciler child failed before pausing", _read_child(r_out))
        # Independent probe from the parent process while process 2 is paused.
        obs["parent_probe_lock_free"] = _reserved_lock_free(receipts_path)
        ev["go"].set()
        obs["writer_attempting"] = ev["attempting"].wait(TIMEOUT)
        # Bounded observation, well below the writer's 5 s busy timeout.
        obs["writer_locked_while_held"] = ev["locked"].wait(BLOCK_OBSERVE)
        obs["writer_done_while_held"] = ev["w_done"].is_set()
        obs["reconciler_still_holding"] = not ev["r_done"].is_set()
        ev["release"].set()
        obs["reconciler_done"] = ev["r_done"].wait(TIMEOUT)
        obs["writer_locked_after_release"] = ev["locked"].wait(TIMEOUT)
        obs["writer_done"] = ev["w_done"].wait(TIMEOUT)
    finally:
        ev["release"].set()
        ev["go"].set()
        for proc in (reconciler, writer):
            if proc.pid is not None:
                proc.join(TIMEOUT)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(TIMEOUT)
    obs["alive"] = [p.name for p in (reconciler, writer) if p.is_alive()]
    obs["exitcodes"] = (reconciler.exitcode, writer.exitcode)
    return obs, _read_child(r_out), _read_child(w_out)


def _assert_two_process_exclusion(obs, r_res, w_res):
    assert obs["alive"] == [], obs
    assert obs["exitcodes"] == (0, 0), obs
    assert r_res and w_res, (r_res, w_res)
    assert r_res["pid"] != os.getpid() and w_res["pid"] != os.getpid() and r_res["pid"] != w_res["pid"]
    assert r_res.get("fired") is True, r_res
    assert r_res.get("lock_free_at_pause") is False, ("process 2 did not hold the receipts lock during its ledger view", r_res)
    assert obs["parent_probe_lock_free"] is False, "receipts lock was free while process 2 was mid-view"
    assert obs["writer_attempting"] is True
    assert obs["writer_locked_while_held"] is False, "process 1 obtained the receipts lock while process 2 held it"
    assert obs["writer_done_while_held"] is False, "process 1 completed while process 2 held the receipts lock"
    assert obs["reconciler_still_holding"] is True
    assert obs["reconciler_done"] and obs["writer_locked_after_release"] and obs["writer_done"], obs
    for res in (r_res, w_res):
        assert "database is locked" not in res.get("error", ""), res
        _assert_no_false_corruption([res.get("error", "")])
        assert res["outcome"] != "crash", res


def test_orig_at15_two_process_constructor_reconciler_excludes_writer(tmp_path):
    """Process 2 constructor-path reconcile holds the receipts lock; process 1 accept waits, then completes."""
    _accepted_store(tmp_path)
    obs, r_res, w_res = _run_two_process_exclusion(
        tmp_path, "constructor",
        dict(task_id="bridge-03", assignment_id="asgn-3", message_id="msg-3", idempotency_key="idem-3"),
    )
    _assert_two_process_exclusion(obs, r_res, w_res)
    assert r_res["outcome"] == "reconciled", r_res
    assert w_res["outcome"] == "accepted", w_res
    assert_joint_consistency(tmp_path / "ledger.db", tmp_path / "receipts.db", expect_ids={"msg-1", "msg-3"})
    assert_fresh_writer_progress(tmp_path / "receipts.db", tmp_path / "ledger.db")


def test_orig_at15_two_process_accept_reconciler_excludes_conflicting_writer(tmp_path):
    """Both processes contest sequence 1; process 2 holds accept()'s lock mid-view, process 1 waits."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    _adapter(ledger, tmp_path / "receipts.db")
    obs, r_res, w_res = _run_two_process_exclusion(
        tmp_path, "accept", dict(message_id="msg-p1", idempotency_key="idem-p1"),
    )
    _assert_two_process_exclusion(obs, r_res, w_res)
    assert r_res["outcome"] == "accepted", r_res
    assert w_res["outcome"] == "error", w_res
    assert any(word in w_res["error"] for word in ALLOWED), w_res
    assert_joint_consistency(tmp_path / "ledger.db", tmp_path / "receipts.db", expect_ids={"msg-p2"})
    assert_fresh_writer_progress(tmp_path / "receipts.db", tmp_path / "ledger.db")
