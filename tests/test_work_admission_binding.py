"""
SKIPPY AI Collaboration Bridge — work-admission binding tests.

Synthetic. Offline. Non-executing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from skippy.collaboration.authorization import (
    AuthorizationPolicy,
    AuthorizationRequest,
    AuthorizationState,
)
from skippy.collaboration.authorization_lifecycle import AuthorizationLifecycle
from skippy.collaboration.chief_identity import (
    ChiefIdentityVerifier,
    canonical_payload,
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
from skippy.collaboration.legacy_halt_migration import AbsentLegacyPolicy
from skippy.collaboration.orchestrator import CollaborationOrchestrator
from skippy.collaboration.protocol import (
    CollaborationProtocol,
    ProtocolError,
)


def _init_authority(path: Path, state="NOT_HALTED", sequence=None) -> None:
    if sequence is None:
        sequence = 1 if state == "HALTED" else 0
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS halt_state (
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
            CREATE TABLE IF NOT EXISTS halt_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                sequence INTEGER,
                detail TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (SUPPORTED_SCHEMA_VERSION,),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, ?, ?, '2026-01-01T00:00:00+00:00', 'test-init')
            """,
            (state, sequence),
        )


def _bound(tmp_path, state="NOT_HALTED"):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated, state)
    gate = CoordinatedHaltGate(coordinated, policy, protocol)
    protocol.require_coordinated_guard(gate)
    return policy, protocol, coordinated, gate, CollaborationOrchestrator(protocol)


def _lifecycle(tmp_path, state="NOT_HALTED"):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated, state)
    verifier.register_public_key("test-key", public)
    created = datetime.now(timezone.utc) - timedelta(minutes=1)
    policy.submit(
        AuthorizationRequest(
            authorization_id="auth-1",
            chief_identity="chief-ref",
            requesting_participant="chief",
            action="merge_pull_request",
            action_digest="digest-a",
            repository="SGGlobal/QLA",
            project_id="qla",
            task_id="task-1",
            expected_sha="a" * 40,
            environment="local",
            created_at=created,
            expires_at=created + timedelta(hours=1),
            state=AuthorizationState.PENDING,
        )
    )
    challenge = verifier.issue_challenge(
        authorization_id="auth-1",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        key_id="test-key",
    )
    gate = CoordinatedHaltGate(coordinated, policy, protocol)
    policy.require_coordinated_guard(gate)
    protocol.require_coordinated_guard(gate)
    lifecycle = AuthorizationLifecycle(verifier, policy, protocol, coordinated_gate=gate)
    return {
        "private": private,
        "verifier": verifier,
        "policy": policy,
        "protocol": protocol,
        "coordinated": coordinated,
        "challenge": challenge,
        "gate": gate,
        "lifecycle": lifecycle,
    }


def _sign(private, fields):
    return private.sign(canonical_payload(fields))


def _status(verifier, challenge_id):
    with sqlite3.connect(verifier.database_path) as connection:
        return connection.execute(
            "SELECT status FROM challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()[0]


def test_ungated_orchestrator_rejects_new_work(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None
    assert orchestrator.router.effects == []


def test_ungated_lifecycle_rejects_evaluation(tmp_path):
    env = _lifecycle(tmp_path)
    ungated = AuthorizationLifecycle(env["verifier"], env["policy"], env["protocol"])
    result = ungated.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.reason == "Work admission rejected"
    assert result.execution_enabled is False
    assert _status(env["verifier"], env["challenge"]["challenge_id"]) == "open"
    assert env["policy"].stored_state("auth-1") == "PENDING"


def test_bound_orchestrator_accepts_valid_work(tmp_path):
    _policy, protocol, _coordinated, _gate, orchestrator = _bound(tmp_path)
    report = orchestrator.run("T1", "objective")
    assert report["status"] == "COMPLETE"
    assert protocol.get_state("T1").value == "COMPLETE"


def test_bound_lifecycle_accepts_valid_evidence(tmp_path):
    env = _lifecycle(tmp_path)
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is True
    assert result.execution_enabled is False


def test_coordinated_halted_rejects_admission(tmp_path):
    _policy, protocol, _coordinated, _gate, orchestrator = _bound(tmp_path, "HALTED")
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None


def test_coordinated_unknown_rejects_admission(tmp_path):
    _policy, protocol, coordinated, _gate, orchestrator = _bound(tmp_path)
    with sqlite3.connect(coordinated) as connection:
        connection.execute("UPDATE halt_state SET sequence = 4")
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None


def test_missing_authority_rejects_admission(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    missing = tmp_path / "missing.db"
    protocol.require_coordinated_guard(CoordinatedHaltGate(missing, policy, protocol))
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        CollaborationOrchestrator(protocol).run("T1", "objective")
    assert not missing.exists()
    assert protocol.get_state("T1") is None


def test_corrupted_authority_rejects_admission(tmp_path):
    _policy, protocol, coordinated, _gate, orchestrator = _bound(tmp_path)
    coordinated.write_bytes(b"corrupt")
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None


def test_failed_migration_rejects_admission(tmp_path):
    policy, protocol, coordinated, _gate, orchestrator = _bound(tmp_path)
    policy.halt()
    protocol.database_path.write_bytes(b"corrupt")
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_legacy_policy_halt_rejects_admission(tmp_path):
    policy, protocol, _coordinated, _gate, orchestrator = _bound(tmp_path)
    policy.halt()
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert policy.is_halted() is True
    assert protocol.get_state("T1") is None


def test_legacy_protocol_halt_rejects_admission(tmp_path):
    _policy, protocol, _coordinated, _gate, orchestrator = _bound(tmp_path)
    protocol.halt()
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.is_halted is True
    assert protocol.get_state("T1") is None


def test_missing_guard_after_binding_rejects(tmp_path):
    _policy, protocol, _coordinated, _gate, orchestrator = _bound(tmp_path)
    protocol._coordinated_guard = None
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None


def test_replaced_guard_rejects(tmp_path):
    policy, protocol, coordinated, _gate, orchestrator = _bound(tmp_path)
    protocol._coordinated_guard = CoordinatedHaltGate(coordinated, policy, protocol)
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None


def test_configuration_drift_rejects(tmp_path):
    _policy, protocol, _coordinated, gate, orchestrator = _bound(tmp_path)
    gate.absent_legacy_policy = AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None


def test_lifecycle_policy_mismatch_rejects(tmp_path):
    env = _lifecycle(tmp_path)
    other = AuthorizationPolicy(tmp_path / "other.db")
    with pytest.raises(ValueError, match="policy mismatch"):
        AuthorizationLifecycle(
            env["verifier"], env["policy"], env["protocol"],
            coordinated_gate=CoordinatedHaltGate(env["coordinated"], other, env["protocol"]),
        )


def test_unexpected_guard_exception_rejects(tmp_path):
    _policy, protocol, _coordinated, gate, orchestrator = _bound(tmp_path)

    def boom():
        raise RuntimeError("secret-token")

    gate.check = boom
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert protocol.get_state("T1") is None


def test_inconsistent_guard_result_rejects(tmp_path):
    env = _lifecycle(tmp_path)

    def bad():
        return GateResult(True, "unsafe", "HALTED", True, False, False)

    env["gate"].check = bad
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.execution_enabled is False
    assert _status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_rejected_admission_has_no_handler_effects(tmp_path):
    _policy, protocol, _coordinated, _gate, orchestrator = _bound(tmp_path, "HALTED")
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        orchestrator.run("T1", "objective")
    assert orchestrator.router.effects == []
    assert protocol.get_state("T1") is None


def test_recovery_remains_available_without_admission(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    status = CollaborationOrchestrator(protocol).recover("missing")
    assert status["recovery"] == "not_found"


def test_protective_halt_remains_available(tmp_path):
    _policy, protocol, _coordinated, _gate, _orchestrator = _bound(tmp_path, "HALTED")
    protocol.halt()
    assert protocol.is_halted is True


def test_reopened_without_rebind_rejects(tmp_path):
    _policy, protocol, _coordinated, _gate, orchestrator = _bound(tmp_path)
    orchestrator.run("T1", "objective")
    reopened = CollaborationProtocol(protocol.database_path)
    assert reopened.coordinated_guard is None
    with pytest.raises(ProtocolError, match="Work admission rejected"):
        CollaborationOrchestrator(reopened).run("T2", "again")
    assert reopened.get_state("T2") is None


def test_execution_disabled_throughout(tmp_path):
    env = _lifecycle(tmp_path)
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.execution_enabled is False
    assert env["gate"].execution_enabled is False


def test_fabricated_gate_cannot_authorize_lifecycle(tmp_path):
    env = _lifecycle(tmp_path)

    class _Fake:
        def __init__(self):
            self.policy = env["policy"]
            self.protocol = env["protocol"]

        def check(self):
            return GateResult(True, "fabricated", "NOT_HALTED", True, False, False)

    with pytest.raises(ValueError, match="must be a CoordinatedHaltGate"):
        AuthorizationLifecycle(
            env["verifier"], env["policy"], env["protocol"], coordinated_gate=_Fake()
        )
    assert _status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_unbound_policy_rejects_lifecycle(tmp_path):
    """Aligned genuine gate, protocol bound, policy not opted in."""
    env = _lifecycle(tmp_path)
    env["policy"]._coordinated_guard = None
    env["policy"]._coordinated_enforcement_required = False
    assert env["gate"].policy is env["policy"]
    assert env["gate"].protocol is env["protocol"]
    assert env["protocol"].coordinated_guard is env["gate"]
    assert env["policy"].coordinated_enforcement_required is False

    result = env["lifecycle"].evaluate(
        env["challenge"], _sign(env["private"], env["challenge"])
    )
    assert result.accepted is False
    assert result.reason == "Work admission rejected"
    assert result.execution_enabled is False
    assert _status(env["verifier"], env["challenge"]["challenge_id"]) == "open"
    assert env["policy"].stored_state("auth-1") == "PENDING"


def test_unbound_protocol_rejects_lifecycle(tmp_path):
    env = _lifecycle(tmp_path)
    env["protocol"]._coordinated_guard = None
    env["protocol"]._coordinated_enforcement_required = False
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.reason == "Work admission rejected"
    assert _status(env["verifier"], env["challenge"]["challenge_id"]) == "open"
    assert env["policy"].stored_state("auth-1") == "PENDING"


def test_aligned_bound_components_succeed(tmp_path):
    env = _lifecycle(tmp_path)
    assert env["policy"].coordinated_guard is env["gate"]
    assert env["protocol"].coordinated_guard is env["gate"]
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is True
    assert result.execution_enabled is False


def test_lifecycle_replaced_guard_identity_rejects(tmp_path):
    env = _lifecycle(tmp_path)
    env["lifecycle"].coordinated_gate = CoordinatedHaltGate(
        env["coordinated"], env["policy"], env["protocol"]
    )
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.reason == "Work admission rejected"
    assert _status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_coordinated_path_drift_rejects_lifecycle(tmp_path):
    env = _lifecycle(tmp_path)
    env["gate"].coordinated_path = tmp_path / "other-coord.db"
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.reason == "Work admission rejected"
    assert _status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_migration_drift_rejects_lifecycle(tmp_path):
    env = _lifecycle(tmp_path)
    env["gate"].absent_legacy_policy = AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.reason == "Work admission rejected"
    assert env["policy"].stored_state("auth-1") == "PENDING"


def test_lifecycle_halted_and_unknown_reject(tmp_path):
    halted_dir = tmp_path / "halted"
    halted_dir.mkdir()
    halted = _lifecycle(halted_dir, state="HALTED")
    result = halted["lifecycle"].evaluate(
        halted["challenge"], _sign(halted["private"], halted["challenge"])
    )
    assert result.accepted is False
    assert result.execution_enabled is False
    assert _status(halted["verifier"], halted["challenge"]["challenge_id"]) == "open"

    unknown_dir = tmp_path / "unknown"
    unknown_dir.mkdir()
    unknown = _lifecycle(unknown_dir)
    with sqlite3.connect(unknown["coordinated"]) as connection:
        connection.execute("UPDATE halt_state SET sequence = 4")
    result = unknown["lifecycle"].evaluate(
        unknown["challenge"], _sign(unknown["private"], unknown["challenge"])
    )
    assert result.accepted is False
    assert result.execution_enabled is False
    assert _status(unknown["verifier"], unknown["challenge"]["challenge_id"]) == "open"


class _ExplosiveStatus(GateResult):
    def __getattribute__(self, name):
        if name == "coordinated_status":
            raise RuntimeError("secret-validation-token")
        return super().__getattribute__(name)


def test_inspect_allowed_halted_is_invalid(tmp_path):
    lifecycle = _lifecycle(tmp_path)["lifecycle"]
    valid, allowed, reason = lifecycle._inspect_gate_result(
        GateResult(True, "unsafe", "HALTED", True, False, False)
    )
    assert valid is False
    assert allowed is False
    assert reason == "Coordinated gate result invalid"


def test_inspect_incomplete_migration_is_invalid(tmp_path):
    lifecycle = _lifecycle(tmp_path)["lifecycle"]
    valid, allowed, reason = lifecycle._inspect_gate_result(
        GateResult(True, "unsafe", "NOT_HALTED", False, False, False)
    )
    assert valid is False
    assert allowed is False
    assert reason == "Coordinated gate result invalid"


def test_inspect_legacy_halt_is_invalid(tmp_path):
    lifecycle = _lifecycle(tmp_path)["lifecycle"]
    for result in (
        GateResult(True, "unsafe", "NOT_HALTED", True, True, False),
        GateResult(True, "unsafe", "NOT_HALTED", True, False, True),
    ):
        valid, allowed, reason = lifecycle._inspect_gate_result(result)
        assert valid is False
        assert allowed is False
        assert reason == "Coordinated gate result invalid"


def test_inspect_malformed_result_type_is_invalid(tmp_path):
    lifecycle = _lifecycle(tmp_path)["lifecycle"]
    valid, allowed, reason = lifecycle._inspect_gate_result({"allowed": True})
    assert valid is False
    assert allowed is False
    assert reason == "Coordinated gate result invalid"


def test_inspect_exception_propagates_for_boundary(tmp_path):
    lifecycle = _lifecycle(tmp_path)["lifecycle"]
    explosive = _ExplosiveStatus(True, "unsafe", "NOT_HALTED", True, False, False)
    with pytest.raises(RuntimeError, match="secret-validation-token"):
        lifecycle._inspect_gate_result(explosive)


def test_inspect_correctly_formed_safe_result(tmp_path):
    lifecycle = _lifecycle(tmp_path)["lifecycle"]
    valid, allowed, reason = lifecycle._inspect_gate_result(
        GateResult(
            True,
            "Safety conditions satisfied; execution disabled",
            "NOT_HALTED",
            True,
            False,
            False,
        )
    )
    assert valid is True
    assert allowed is True
    assert reason == "Safety conditions satisfied; execution disabled"
