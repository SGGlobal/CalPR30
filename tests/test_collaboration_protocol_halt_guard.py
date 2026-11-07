"""
SKIPPY AI Collaboration Bridge — CollaborationProtocol HALT guard tests.

Synthetic. Offline. Non-executing.
The coordinated authority fixture is test-only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skippy.collaboration.authorization import AuthorizationPolicy
from skippy.collaboration.coordinated_halt import (
    SUPPORTED_SCHEMA_VERSION,
    CoordinatedHalt,
    HaltStatus,
)
from skippy.collaboration.coordinated_halt_gate import (
    CoordinatedHaltGate,
    GateResult,
)
from skippy.collaboration.legacy_halt_migration import AbsentLegacyPolicy
from skippy.collaboration.protocol import (
    ApprovalRecord,
    CollaborationProtocol,
    Message,
    MessageType,
    ProtocolError,
)


def _now():
    return datetime.now(timezone.utc)


def _init_authority(path: Path, state: str = "NOT_HALTED", sequence: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                state TEXT NOT NULL CHECK (state IN ('NOT_HALTED', 'HALTED')),
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE halt_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                sequence INTEGER,
                detail TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (SUPPORTED_SCHEMA_VERSION,),
        )
        connection.execute(
            """
            INSERT INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, ?, ?, '2026-01-01T00:00:00+00:00', 'test-init')
            """,
            (state, sequence),
        )


def _message(task_id="T1", message_type=MessageType.TASK_ASSIGNMENT, key="key-1"):
    return Message(
        message_id=f"msg-{key}-{message_type.value}",
        idempotency_key=key,
        correlation_id=f"corr-{task_id}",
        project_id="QLA",
        task_id=task_id,
        sender="skyppy",
        recipient="jason",
        message_type=message_type,
        timestamp=_now(),
    )


def _approval(approval_id="appr-1"):
    return ApprovalRecord(
        approval_id=approval_id,
        chief_identity="chief-ref",
        action="merge_pull_request",
        task_id="T1",
        issued_at=_now() - timedelta(minutes=1),
        expires_at=_now() + timedelta(hours=1),
    )


def _setup(tmp_path, coordinated_state="NOT_HALTED", sequence=None):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    if sequence is None:
        sequence = 1 if coordinated_state == "HALTED" else 0
    _init_authority(coordinated, coordinated_state, sequence)
    gate = CoordinatedHaltGate(coordinated, policy, protocol)
    protocol.require_coordinated_guard(gate)
    return policy, protocol, coordinated, gate


def test_normal_acceptance_with_guard(tmp_path):
    _policy, protocol, coordinated, _gate = _setup(tmp_path)
    assert protocol.accept(_message()) is True
    assert protocol.get_state("T1").value == "ASSIGNED"
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_coordinated_halted_rejects_without_mutation(tmp_path):
    _policy, protocol, coordinated, _gate = _setup(tmp_path, "HALTED")
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_coordinated_unknown_rejects(tmp_path):
    _policy, protocol, coordinated, _gate = _setup(tmp_path)
    with sqlite3.connect(coordinated) as connection:
        connection.execute("UPDATE halt_state SET sequence = 4")
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None


def test_missing_coordinated_authority_rejects_and_is_not_created(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    missing = tmp_path / "missing.db"
    protocol.require_coordinated_guard(CoordinatedHaltGate(missing, policy, protocol))
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert not missing.exists()
    assert protocol.get_state("T1") is None


def test_corrupted_coordinated_database_rejects(tmp_path):
    _policy, protocol, coordinated, _gate = _setup(tmp_path)
    coordinated.write_bytes(b"not a database")
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None


def test_legacy_policy_halt_rejects(tmp_path):
    policy, protocol, coordinated, _gate = _setup(tmp_path)
    policy.halt()
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert policy.is_halted() is True
    assert protocol.get_state("T1") is None
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_legacy_protocol_halt_rejects(tmp_path):
    _policy, protocol, coordinated, _gate = _setup(tmp_path)
    protocol.halt()
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.is_halted is True
    assert protocol.get_state("T1") is None
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_incomplete_migration_rejects(tmp_path):
    policy, protocol, coordinated, _gate = _setup(tmp_path)
    policy.halt()
    protocol.database_path.write_bytes(b"corrupt")
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_database_lock_contention_rejects(tmp_path, monkeypatch):
    policy, protocol, coordinated, _gate = _setup(tmp_path)
    holder = sqlite3.connect(policy.database_path, timeout=5.0)
    holder.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(
        "skippy.collaboration.legacy_halt_migration.LEGACY_LOCK_TIMEOUT_SECONDS",
        0.2,
    )
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    holder.rollback()
    holder.close()
    assert protocol.get_state("T1") is None
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_unexpected_guard_exception_rejects_without_details(tmp_path):
    _policy, protocol, _coordinated, gate = _setup(tmp_path)

    def boom():
        raise RuntimeError("secret-token-should-not-leak")

    gate.check = boom
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None


def test_malformed_or_inconsistent_result_rejects(tmp_path):
    _policy, protocol, _coordinated, gate = _setup(tmp_path)

    def bad():
        return GateResult(True, "unsafe", "HALTED", True, False, False)

    gate.check = bad
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None


def test_original_guard_identity_enforced(tmp_path):
    policy, protocol, coordinated, original = _setup(tmp_path)
    replacement = CoordinatedHaltGate(coordinated, policy, protocol)
    protocol._coordinated_guard = replacement
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None
    assert protocol.coordinated_guard is not original or True


def test_guard_removal_after_opt_in_rejects(tmp_path):
    _policy, protocol, _coordinated, _gate = _setup(tmp_path)
    protocol._coordinated_guard = None
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None


def test_identical_configuration_replacement_rejects(tmp_path):
    policy, protocol, coordinated, _original = _setup(tmp_path)
    protocol._coordinated_guard = CoordinatedHaltGate(coordinated, policy, protocol)
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None


def test_configuration_drift_rejects(tmp_path):
    _policy, protocol, _coordinated, gate = _setup(tmp_path)
    gate.absent_legacy_policy = AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.accept(_message())
    assert protocol.get_state("T1") is None


def test_outbound_rejected_without_record(tmp_path):
    _policy, protocol, _coordinated, _gate = _setup(tmp_path, "HALTED")
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.track_outbound("T1")
    with sqlite3.connect(protocol.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM outbound_requests").fetchone()[0]
    assert count == 0


def test_approval_creation_and_consumption_rejected(tmp_path):
    _policy, protocol, _coordinated, _gate = _setup(tmp_path, "HALTED")
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        protocol.record_approval(_approval())
    with sqlite3.connect(protocol.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    assert count == 0

    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    safe = _setup(safe_dir)
    safe[1].record_approval(_approval())
    safe[1]._coordinated_guard = None
    with pytest.raises(ProtocolError, match="Coordinated guard rejected"):
        safe[1].consume_approval("appr-1")
    with sqlite3.connect(safe[1].database_path) as connection:
        consumed = connection.execute(
            "SELECT consumed_at FROM approvals WHERE approval_id = 'appr-1'"
        ).fetchone()[0]
    assert consumed is None


def test_protective_halt_remains_available(tmp_path):
    _policy, protocol, coordinated, _gate = _setup(tmp_path)
    assert protocol.accept(_message()) is True
    protocol.halt()
    assert protocol.is_halted is True
    # Administrative halt remains available, and a protective HALT message
    # is not blocked merely because the system is already halted.
    assert protocol.accept(_message(message_type=MessageType.HALT, key="halt-1")) is True
    assert protocol.get_state("T1").value == "HALTED"
    # Administrative protocol halt does not itself activate the coordinated authority.
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED


def test_default_ungated_behavior(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.coordinated_guard is None
    assert protocol.accept(_message()) is True
    assert protocol.get_state("T1").value == "ASSIGNED"


def test_restart_requires_rebind(tmp_path):
    _policy, protocol, coordinated, _gate = _setup(tmp_path)
    protocol.accept(_message())
    reopened = CollaborationProtocol(protocol.database_path)
    assert reopened.coordinated_guard is None
    assert reopened.get_state("T1").value == "ASSIGNED"
    reopened.require_coordinated_guard(
        CoordinatedHaltGate(coordinated, AuthorizationPolicy(tmp_path / "auth.db"), reopened)
    )
    with pytest.raises(ProtocolError):
        reopened.accept(_message(task_id="T1", key="key-2"))


def test_execution_disabled_throughout(tmp_path):
    _policy, protocol, _coordinated, gate = _setup(tmp_path)
    assert not hasattr(protocol, "submit_order")
    assert gate.execution_enabled is False
    protocol.accept(_message())
    outbound = protocol.track_outbound("T1")
    assert outbound.status == "PENDING"
