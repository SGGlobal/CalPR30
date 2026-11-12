"""Connection-lifecycle regression tests for the offline adapter and admission gate.

Synthetic. Offline. Non-executing. execution_enabled remains False.

Connections are tracked by owner, not by garbage collection:
- Component-owned connections are recorded by replacing the ``sqlite3``
  name inside the component module with a delegating tracker, so only
  ``sqlite3.connect`` calls made by that module are recorded.
- Connections the adapter obtains through ``ledger._connect()`` are recorded
  only when the immediate caller is the adapter module. Connections the
  ledger opens for its own internal work are not recorded.
The tracker holds strong references, so a leaked connection cannot be
closed behind the test's back by the garbage collector.
"""

import sqlite3
import sys

import pytest

import skippy.collaboration.offline_admission_gate as gate_module
import skippy.collaboration.offline_authenticated_adapter as adapter_module
from skippy.collaboration.checkpoint_ledger import CheckpointLedger
from skippy.collaboration.offline_admission_gate import AdmissionError
from skippy.collaboration.offline_authenticated_adapter import (
    OfflineAdapterError,
    OfflineAuthenticatedAdapter,
)
from test_authenticated_envelope import _envelope as _adapter_envelope
from test_authenticated_envelope import _now as _adapter_now
from test_offline_admission_gate import (
    UnavailableHalt,
    _envelope as _gate_envelope,
    _gate,
    _key_pair,
    _now as _gate_now,
)


class _TrackingSqlite:
    """Delegates to the real sqlite3 module and records connect() results."""

    def __init__(self, real):
        self._real = real
        self.opened = []

    def connect(self, *args, **kwargs):
        connection = self._real.connect(*args, **kwargs)
        self.opened.append(connection)
        return connection

    def __getattr__(self, name):
        return getattr(self._real, name)


def _is_closed(connection) -> bool:
    try:
        connection.total_changes
    except sqlite3.ProgrammingError:
        return True
    return False


def _open(connections):
    return [c for c in connections if not _is_closed(c)]


@pytest.fixture
def gate_tracker(monkeypatch):
    tracker = _TrackingSqlite(sqlite3)
    monkeypatch.setattr(gate_module, "sqlite3", tracker)
    return tracker


@pytest.fixture
def adapter_tracker(monkeypatch):
    tracker = _TrackingSqlite(sqlite3)
    monkeypatch.setattr(adapter_module, "sqlite3", tracker)
    return tracker


def _track_adapter_ledger_connections(monkeypatch, ledger):
    """Record ledger._connect() results only when the adapter is the caller."""
    opened = []
    real_connect = ledger._connect

    def tracked_connect():
        connection = real_connect()
        if sys._getframe(1).f_globals.get("__name__") == adapter_module.__name__:
            opened.append(connection)
        return connection

    monkeypatch.setattr(ledger, "_connect", tracked_connect)
    return opened


def _adapter(tmp_path, monkeypatch, *, trusted_fixture=True, halt_check=lambda: False):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger_opened = _track_adapter_ledger_connections(monkeypatch, ledger)
    adapter = OfflineAuthenticatedAdapter(
        ledger,
        tmp_path / "receipts.db",
        trusted_fixture=trusted_fixture,
        halt_check=halt_check,
    )
    return ledger, adapter, ledger_opened


# --- tracker sanity -------------------------------------------------------


def test_open_connection_is_detected(tmp_path):
    connection = sqlite3.connect(tmp_path / "probe.db")
    assert not _is_closed(connection)
    connection.close()
    assert _is_closed(connection)


def test_tracker_ignores_ledger_module_connections(tmp_path, adapter_tracker):
    CheckpointLedger(tmp_path / "ledger.db")
    assert adapter_tracker.opened == []


# --- admission gate -------------------------------------------------------


def test_gate_construction_closes_connections(tmp_path, gate_tracker):
    _gate(tmp_path, _key_pair())
    assert len(gate_tracker.opened) >= 2
    assert _open(gate_tracker.opened) == []


def test_gate_success_closes_connections(tmp_path, gate_tracker):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    before = len(gate_tracker.opened)
    assert gate.reserve(_gate_envelope(sk), _gate_now()).outcome == "reserved"
    assert len(gate_tracker.opened) > before
    assert _open(gate_tracker.opened) == []


def test_gate_duplicate_closes_connections(tmp_path, gate_tracker):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    gate.reserve(_gate_envelope(sk), _gate_now())
    assert gate.reserve(_gate_envelope(sk), _gate_now()).outcome == "duplicate"
    assert _open(gate_tracker.opened) == []


def test_gate_conflict_rejection_closes_connections(tmp_path, gate_tracker):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    gate.reserve(_gate_envelope(sk), _gate_now())
    other = _gate_envelope(sk, message_id="m1", idempotency_key="i2", sequence=2)
    assert gate.reserve(other, _gate_now()).outcome == "rejected"
    assert _open(gate_tracker.opened) == []


def test_gate_halt_rejection_closes_connections(tmp_path, gate_tracker):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk), halt=UnavailableHalt())
    before = len(gate_tracker.opened)
    assert gate.reserve(_gate_envelope(sk), _gate_now()).outcome == "rejected"
    assert len(gate_tracker.opened) > before
    assert _open(gate_tracker.opened) == []


def _insert_pending(db_path):
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO reservations (
                    project_id, task_id, checkpoint_id, assignment_id,
                    message_id, idempotency_key, sender, recipient, role, payload_digest,
                    sequence, state, recorded_at
                ) VALUES ('QLA','t1','c1','a1','old','oldk','chief','jason','chief',?,1,
                          'PENDING', '2026-11-08T12:00:00')
                """,
                (b"\x00" * 32,),
            )
    finally:
        connection.close()


def test_gate_unresolved_block_on_construction_closes_connections(tmp_path, gate_tracker):
    sk, pk = _key_pair()
    _gate(tmp_path, (sk, pk))
    _insert_pending(tmp_path / "admit.db")
    with pytest.raises(AdmissionError, match="PENDING"):
        _gate(tmp_path, (sk, pk))
    assert _open(gate_tracker.opened) == []


def test_gate_unresolved_block_on_reserve_closes_connections(tmp_path, gate_tracker):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    _insert_pending(tmp_path / "admit.db")
    with pytest.raises(AdmissionError, match="PENDING"):
        gate.reserve(_gate_envelope(sk), _gate_now())
    assert _open(gate_tracker.opened) == []


def test_gate_storage_exception_closes_connections(tmp_path, gate_tracker):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    connection = sqlite3.connect(tmp_path / "admit.db")
    try:
        with connection:
            connection.execute(
                """
                CREATE TRIGGER injected_insert_failure BEFORE INSERT ON reservations
                BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END
                """
            )
    finally:
        connection.close()
    with pytest.raises(AdmissionError, match="storage failure"):
        gate.reserve(_gate_envelope(sk), _gate_now())
    assert _open(gate_tracker.opened) == []


# --- offline authenticated adapter ----------------------------------------


def test_adapter_construction_closes_connections(tmp_path, monkeypatch, adapter_tracker):
    _, _, ledger_opened = _adapter(tmp_path, monkeypatch)
    assert len(adapter_tracker.opened) >= 3
    assert len(ledger_opened) == 1  # _reconcile only; ledger-internal calls excluded
    assert _open(adapter_tracker.opened) == []
    assert _open(ledger_opened) == []


def test_adapter_success_closes_connections(tmp_path, monkeypatch, adapter_tracker):
    _, adapter, ledger_opened = _adapter(tmp_path, monkeypatch)
    assert adapter.accept(_adapter_envelope(), now=_adapter_now()) == "accepted"
    # construction _reconcile + accept _reconcile + accept revision lookup;
    # record_event's own ledger connections are not adapter-owned
    assert len(ledger_opened) == 3
    assert _open(adapter_tracker.opened) == []
    assert _open(ledger_opened) == []


def test_adapter_halt_rejection_closes_connections(tmp_path, monkeypatch, adapter_tracker):
    _, adapter, ledger_opened = _adapter(tmp_path, monkeypatch, halt_check=lambda: True)
    with pytest.raises(OfflineAdapterError, match="HALTED"):
        adapter.accept(_adapter_envelope(), now=_adapter_now())
    assert _open(adapter_tracker.opened) == []
    assert _open(ledger_opened) == []


def test_adapter_untrusted_rejection_closes_connections(tmp_path, monkeypatch, adapter_tracker):
    _, adapter, ledger_opened = _adapter(tmp_path, monkeypatch, trusted_fixture=False)
    with pytest.raises(OfflineAdapterError, match="trusted fixture"):
        adapter.accept(_adapter_envelope(), now=_adapter_now())
    assert _open(adapter_tracker.opened) == []
    assert _open(ledger_opened) == []


def test_adapter_unresolved_intent_on_restart_closes_connections(
    tmp_path, monkeypatch, adapter_tracker
):
    ledger, _, ledger_opened = _adapter(tmp_path, monkeypatch)
    connection = sqlite3.connect(tmp_path / "receipts.db")
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO intent_journal (
                    message_id, idempotency_key, project_id, task_id,
                    checkpoint_id, assignment_id, sequence, payload_digest,
                    state, recorded_at
                ) VALUES ('pending-msg', 'pending-idem', 'QLA', 't', 'c', 'a', 1, 'd',
                          'PENDING', '2026-11-08T12:00:00')
                """
            )
    finally:
        connection.close()
    with pytest.raises(OfflineAdapterError, match="Unresolved|PENDING|Active"):
        OfflineAuthenticatedAdapter(
            ledger, tmp_path / "receipts.db", trusted_fixture=True, halt_check=lambda: False
        )
    assert _open(adapter_tracker.opened) == []
    assert _open(ledger_opened) == []


def test_adapter_partial_write_closes_connections(tmp_path, monkeypatch, adapter_tracker):
    _, adapter, ledger_opened = _adapter(tmp_path, monkeypatch)
    original = adapter._record_receipt_in

    def failing_receipt(connection, envelope, outcome, reason, now):
        if outcome == "accepted":
            raise RuntimeError("simulated receipt failure")
        return original(connection, envelope, outcome, reason, now)

    monkeypatch.setattr(adapter, "_record_receipt_in", failing_receipt)
    with pytest.raises(OfflineAdapterError, match="Partial write"):
        adapter.accept(_adapter_envelope(), now=_adapter_now())
    assert _open(adapter_tracker.opened) == []
    assert _open(ledger_opened) == []
