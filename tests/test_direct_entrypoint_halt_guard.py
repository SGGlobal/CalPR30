"""
SKIPPY AI Collaboration Bridge — Direct AuthorizationPolicy HALT guard tests.

Synthetic. Offline. Non-executing.
The coordinated authority fixture is test-only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skippy.collaboration.authorization import (
    AuthorizationDecision,
    AuthorizationError,
    AuthorizationPolicy,
    AuthorizationRequest,
    AuthorizationState,
)
from skippy.collaboration.coordinated_halt import (
    SUPPORTED_SCHEMA_VERSION,
    CoordinatedHalt,
    HaltStatus,
)
from skippy.collaboration.coordinated_halt_gate import (
    CoordinatedHaltGate,
    GateResult,
)
from skippy.collaboration.protocol import CollaborationProtocol


def _now():
    return datetime.now(timezone.utc)


def _init_authority(path: Path, state: str = "NOT_HALTED", sequence: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
            """
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


def _request(**overrides):
    values = {
        "authorization_id": "auth-1",
        "chief_identity": "chief-ref",
        "requesting_participant": "chief",
        "action": "merge_pull_request",
        "action_digest": "digest-a",
        "repository": "SGGlobal/QLA",
        "project_id": "qla",
        "task_id": "task-1",
        "expected_sha": "a" * 40,
        "environment": "local",
        "created_at": _now() - timedelta(minutes=1),
        "expires_at": _now() + timedelta(hours=1),
        "state": AuthorizationState.PENDING,
    }
    values.update(overrides)
    return AuthorizationRequest(**values)


def _setup(tmp_path, coordinated_state="NOT_HALTED", sequence=None):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    if sequence is None:
        sequence = 1 if coordinated_state == "HALTED" else 0
    _init_authority(coordinated, coordinated_state, sequence)
    gate = CoordinatedHaltGate(coordinated, policy, protocol)
    policy.require_coordinated_guard(gate)
    return policy, protocol, coordinated, gate


def _decide(policy, authorization_id="auth-1"):
    return policy.decide(
        authorization_id,
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="a" * 40,
        environment="local",
    )


def test_successful_synthetic_operations_with_guard(tmp_path):
    policy, _protocol, coordinated, _gate = _setup(tmp_path)
    policy.submit(_request())
    decision = _decide(policy)
    evidence = policy.evaluate_evidence(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="a" * 40,
        environment="local",
    )
    assert decision.execution_enabled is False
    assert evidence.execution_enabled is False
    assert evidence.state is AuthorizationState.PENDING
    assert policy.stored_state("auth-1") == "PENDING"
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_coordinated_halted_rejects_without_mutation(tmp_path):
    policy, _protocol, coordinated, _gate = _setup(tmp_path, "HALTED")
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None
    decision = _decide(policy)
    assert decision.allowed is False
    assert decision.execution_enabled is False
    assert decision.reason == "Coordinated guard rejected"
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_coordinated_unknown_rejects(tmp_path):
    policy, _protocol, coordinated, _gate = _setup(tmp_path)
    with sqlite3.connect(coordinated) as connection:
        connection.execute("UPDATE halt_state SET sequence = 4 WHERE state = 'NOT_HALTED'")
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None


def test_missing_coordinated_authority_rejects_and_is_not_created(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    missing = tmp_path / "missing.db"
    policy.require_coordinated_guard(CoordinatedHaltGate(missing, policy, protocol))
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert not missing.exists()
    assert policy.stored_state("auth-1") is None


def test_corrupted_coordinated_database_rejects(tmp_path):
    policy, _protocol, coordinated, _gate = _setup(tmp_path)
    coordinated.write_bytes(b"not a database")
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None


def test_legacy_policy_halt_rejects_and_preserves_flag(tmp_path):
    policy, _protocol, coordinated, _gate = _setup(tmp_path)
    policy.halt()
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.is_halted() is True
    assert policy.stored_state("auth-1") is None
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_legacy_protocol_halt_rejects(tmp_path):
    policy, protocol, coordinated, _gate = _setup(tmp_path)
    protocol.halt()
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert protocol.is_halted is True
    assert policy.stored_state("auth-1") is None
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_partial_migration_failure_rejects(tmp_path):
    policy, protocol, coordinated, _gate = _setup(tmp_path)
    policy.halt()
    protocol.database_path.write_bytes(b"corrupt")
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
    assert policy.stored_state("auth-1") is None


def test_database_lock_contention_rejects(tmp_path, monkeypatch):
    policy, _protocol, coordinated, _gate = _setup(tmp_path)
    holder = sqlite3.connect(policy.database_path, timeout=5.0)
    holder.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(
        "skippy.collaboration.legacy_halt_migration.LEGACY_LOCK_TIMEOUT_SECONDS",
        0.2,
    )
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    holder.rollback()
    holder.close()
    assert policy.stored_state("auth-1") is None
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


class _RaisingGate:
    def __init__(self, policy):
        self.policy = policy

    def check(self):
        raise RuntimeError("secret-token-should-not-leak")


def test_unexpected_guard_exception_rejects_without_details(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    with pytest.raises(AuthorizationError, match="Invalid coordinated guard"):
        policy.require_coordinated_guard(_RaisingGate(policy))

    bound_dir = tmp_path / "bound"
    bound_dir.mkdir()
    bound, _protocol, _coordinated, gate = _setup(bound_dir)
    def boom():
        raise RuntimeError("secret-token-should-not-leak")
    gate.check = boom
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        bound.submit(_request())
    assert bound.stored_state("auth-1") is None


def test_mismatched_component_configuration_rejected(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    other = AuthorizationPolicy(tmp_path / "other.db")
    mismatched = CoordinatedHaltGate(coordinated, other, protocol)
    with pytest.raises(AuthorizationError, match="policy mismatch"):
        policy.require_coordinated_guard(mismatched)


def test_guard_cannot_be_replaced(tmp_path):
    policy, protocol, coordinated, gate = _setup(tmp_path)
    other = CoordinatedHaltGate(coordinated, policy, protocol)
    with pytest.raises(AuthorizationError, match="cannot be replaced"):
        policy.require_coordinated_guard(other)
    assert policy.coordinated_guard is gate


def test_malformed_or_contradictory_gate_result_rejects(tmp_path):
    policy, protocol, _coordinated, _gate = _setup(tmp_path)

    class _BadGate:
        def __init__(self, bound, result):
            self.policy = bound
            self.result = result

        def check(self):
            return self.result

    # Supported binder rejects non-gate types. Inject only to test result validation.
    contradictory = GateResult(True, "unsafe", "HALTED", True, False, False)
    policy._coordinated_guard = _BadGate(policy, contradictory)
    decision = _decide(policy)
    assert decision.allowed is False
    assert decision.execution_enabled is False
    assert decision.reason == "Coordinated guard rejected"
    assert policy.stored_state("auth-1") is None


def test_consume_and_decide_do_not_mutate_on_rejection(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    policy.submit(_request(state=AuthorizationState.APPROVED))
    policy.require_coordinated_guard(CoordinatedHaltGate(coordinated, policy, protocol))
    with sqlite3.connect(coordinated) as connection:
        connection.execute(
            "UPDATE halt_state SET state = 'HALTED', sequence = 1"
        )
    before = policy.stored_state("auth-1")
    decision = _decide(policy)
    consumed = policy.consume("auth-1")
    assert decision.execution_enabled is False
    assert consumed.execution_enabled is False
    assert consumed.state is AuthorizationState.DENIED
    assert policy.stored_state("auth-1") == before
    assert "Coordinated guard rejected" in policy.events_for("auth-1")


def _gate_path(tmp_path):
    return tmp_path / "coord.db"


def test_repeated_operations_and_restart(tmp_path):
    policy, protocol, coordinated, _gate = _setup(tmp_path)
    policy.submit(_request())
    reopened = AuthorizationPolicy(policy.database_path)
    # Guard is in-memory opt-in, not persisted. Reopened default remains ungated.
    assert reopened.coordinated_guard is None
    reopened.require_coordinated_guard(CoordinatedHaltGate(coordinated, reopened, protocol))
    decision = _decide(reopened)
    assert decision.execution_enabled is False
    assert reopened.stored_state("auth-1") == "PENDING"


def test_default_behavior_unchanged(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    assert policy.coordinated_guard is None
    policy.submit(_request())
    decision = _decide(policy)
    assert isinstance(decision, AuthorizationDecision)
    assert decision.execution_enabled is False
    assert policy.stored_state("auth-1") == "PENDING"


def test_administrative_halt_and_revoke_remain_available(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated, "HALTED", 1)
    policy.submit(_request())
    policy.require_coordinated_guard(CoordinatedHaltGate(coordinated, policy, protocol))
    policy.halt()
    assert policy.is_halted() is True
    decision = policy.revoke("auth-1")
    assert decision.execution_enabled is False
    assert policy.stored_state("auth-1") == "REVOKED"


def test_execution_disabled_throughout(tmp_path):
    policy, _protocol, _coordinated, _gate = _setup(tmp_path)
    policy.submit(_request())
    for decision in (
        _decide(policy),
        policy.evaluate_evidence(
            "auth-1",
            action="merge_pull_request",
            repository="SGGlobal/QLA",
            project_id="qla",
            task_id="task-1",
            action_digest="digest-a",
            expected_sha="a" * 40,
            environment="local",
        ),
    ):
        assert decision.execution_enabled is False


def test_unchanged_guard_allows_normal_operation(tmp_path):
    policy, _protocol, _coordinated, _gate = _setup(tmp_path)
    policy.submit(_request())
    decision = _decide(policy)
    assert decision.execution_enabled is False
    assert policy.stored_state("auth-1") == "PENDING"


def test_unexpected_guard_removal_rejects(tmp_path):
    policy, _protocol, _coordinated, _gate = _setup(tmp_path)
    policy._coordinated_guard = None  # unsupported loss after opt-in
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None
    decision = _decide(policy)
    assert decision.allowed is False
    assert decision.execution_enabled is False


def test_bound_protocol_replacement_rejects(tmp_path):
    policy, protocol, coordinated, gate = _setup(tmp_path)
    other = CollaborationProtocol(tmp_path / "other-bridge.db")
    gate.protocol = other
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None
    assert protocol.is_halted is False


def test_coordinated_path_modification_rejects(tmp_path):
    policy, _protocol, coordinated, gate = _setup(tmp_path)
    gate.coordinated_path = tmp_path / "other-coord.db"
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None
    assert coordinated.exists()


def test_migration_setting_modification_rejects(tmp_path):
    from skippy.collaboration.legacy_halt_migration import AbsentLegacyPolicy

    policy, _protocol, _coordinated, gate = _setup(tmp_path)
    gate.absent_legacy_policy = AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None


def test_guard_configuration_mismatch_after_binding_rejects(tmp_path):
    policy, _protocol, _coordinated, gate = _setup(tmp_path)
    gate.policy = AuthorizationPolicy(tmp_path / "other.db")
    with pytest.raises(AuthorizationError, match="Coordinated guard rejected"):
        policy.submit(_request())
    assert policy.stored_state("auth-1") is None
