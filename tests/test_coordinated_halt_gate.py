"""
SKIPPY AI Collaboration Bridge — Coordinated HALT integration gate tests.

Synthetic. Offline. Non-executing.
The coordinated authority fixture is test-only. It is not a production bootstrap.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
from skippy.collaboration.protocol import CollaborationProtocol


def _now():
    return datetime.now(timezone.utc)


def _init_authority(path: Path, state: str = "NOT_HALTED", sequence: int = 0) -> None:
    """Test-only fixture. Not a production initialization API."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes_raw()


def _sign(private, fields):
    return private.sign(canonical_payload(fields))


def _setup(tmp_path, coordinated_state="NOT_HALTED", sequence=None):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    if sequence is None:
        sequence = 1 if coordinated_state == "HALTED" else 0
    _init_authority(coordinated, state=coordinated_state, sequence=sequence)
    verifier.register_public_key("test-key", public)

    created = _now() - timedelta(minutes=1)
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


def _challenge_status(verifier, challenge_id) -> str:
    with sqlite3.connect(verifier.database_path) as connection:
        row = connection.execute(
            "SELECT status FROM challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
    return row[0]


def test_successful_synthetic_evaluation_when_safe(tmp_path):
    env = _setup(tmp_path)
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))

    assert result.accepted is True
    assert result.identity_verified is True
    assert result.execution_enabled is False
    assert "execution disabled" in result.reason
    assert env["policy"].stored_state("auth-1") == "PENDING"
    status, sequence = CoordinatedHalt(env["coordinated"]).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_coordinated_halted_rejects(tmp_path):
    env = _setup(tmp_path, coordinated_state="HALTED")
    gate = env["gate"].check()
    assert gate.allowed is False
    assert gate.execution_enabled is False
    assert "HALTED" in gate.reason

    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.identity_verified is False
    assert result.execution_enabled is False
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_coordinated_unknown_rejects(tmp_path):
    env = _setup(tmp_path)
    with sqlite3.connect(env["coordinated"]) as connection:
        connection.execute("UPDATE halt_state SET state = 'NOT_HALTED', sequence = 4")

    gate = env["gate"].check()
    assert gate.allowed is False
    assert gate.execution_enabled is False
    assert "UNKNOWN" in gate.reason or "Migration failed" in gate.reason


def test_missing_coordinated_authority_rejects_and_is_not_created(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    missing = tmp_path / "missing-coord.db"
    gate = CoordinatedHaltGate(missing, policy, protocol)

    result = gate.check()
    assert result.allowed is False
    assert result.migration_completed is False
    assert result.execution_enabled is False
    assert "Migration failed" in result.reason
    assert not missing.exists()


def test_corrupted_coordinated_authority_rejects(tmp_path):
    env = _setup(tmp_path)
    env["coordinated"].write_bytes(b"not a database")

    result = env["gate"].check()
    assert result.allowed is False
    assert result.execution_enabled is False
    assert result.migration_completed is False


def test_legacy_halt_after_migration_is_not_bypassed(tmp_path):
    env = _setup(tmp_path)
    env["policy"].halt()

    result = env["gate"].check()
    assert result.allowed is False
    assert result.execution_enabled is False
    # Legacy flag remains authoritative and is preserved.
    assert env["policy"].is_halted() is True
    status, _sequence = CoordinatedHalt(env["coordinated"]).read_status()
    assert status is HaltStatus.HALTED

    lifecycle = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert lifecycle.accepted is False
    assert lifecycle.execution_enabled is False
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_partial_migration_failure_rejects(tmp_path):
    env = _setup(tmp_path)
    env["policy"].halt()
    env["protocol"].database_path.write_bytes(b"corrupt protocol")

    result = env["gate"].check()
    assert result.allowed is False
    assert result.migration_completed is False
    assert result.execution_enabled is False
    status, sequence = CoordinatedHalt(env["coordinated"]).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_database_lock_contention_rejects(tmp_path, monkeypatch):
    env = _setup(tmp_path)
    holder = sqlite3.connect(env["policy"].database_path, timeout=5.0)
    holder.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(
        "skippy.collaboration.legacy_halt_migration.LEGACY_LOCK_TIMEOUT_SECONDS",
        0.2,
    )

    result = env["gate"].check()
    assert result.allowed is False
    assert result.execution_enabled is False
    assert result.migration_completed is False
    holder.rollback()
    holder.close()

    status, sequence = CoordinatedHalt(env["coordinated"]).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_repeated_evaluation_and_restart(tmp_path):
    env = _setup(tmp_path)
    first = env["gate"].check()
    second = env["gate"].check()
    assert first.allowed is True
    assert second.allowed is True
    assert first.execution_enabled is False
    assert second.execution_enabled is False

    reopened = CoordinatedHaltGate(env["coordinated"], env["policy"], env["protocol"])
    third = reopened.check()
    assert third.allowed is True
    assert third.execution_enabled is False
    status, sequence = CoordinatedHalt(env["coordinated"]).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_identity_verification_requirements_preserved(tmp_path):
    env = _setup(tmp_path)
    other, _ = _keypair()

    rejected = env["lifecycle"].evaluate(env["challenge"], _sign(other, env["challenge"]))
    assert rejected.accepted is False
    assert rejected.identity_verified is False
    assert rejected.execution_enabled is False
    # Invalid signature must not consume the challenge.
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"

    accepted = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert accepted.accepted is True
    assert accepted.identity_verified is True
    assert accepted.execution_enabled is False
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "consumed"


def test_failed_gate_does_not_consume_challenge(tmp_path):
    env = _setup(tmp_path, coordinated_state="HALTED")
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.execution_enabled is False
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_default_lifecycle_unchanged_without_gate(tmp_path):
    env = _setup(tmp_path)
    default = AuthorizationLifecycle(env["verifier"], env["policy"], env["protocol"])
    result = default.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is True
    assert result.execution_enabled is False
    assert default.coordinated_gate is None


def test_gate_result_execution_disabled_cannot_be_overridden():
    result = GateResult(True, "ok", "NOT_HALTED", True, False, False)
    assert result.execution_enabled is False
    with pytest.raises(TypeError):
        GateResult(
            True,
            "ok",
            "NOT_HALTED",
            True,
            False,
            False,
            execution_enabled=True,
        )


def test_caller_supplied_migration_result_is_not_authorization(tmp_path):
    """The gate has no API that accepts a migration result as permission."""
    env = _setup(tmp_path, coordinated_state="HALTED")
    assert not hasattr(env["gate"], "accept_migration_result")
    result = env["gate"].check()
    assert result.allowed is False
    assert result.execution_enabled is False


def test_no_protected_execution_capability(tmp_path):
    env = _setup(tmp_path)
    assert env["gate"].execution_enabled is False
    result = env["gate"].check()
    assert result.execution_enabled is False
    assert not hasattr(env["gate"], "submit_order")
    assert not hasattr(env["gate"], "deploy")


class _RaisingGate:
    def __init__(self, policy, protocol, message="secret-token-should-not-leak"):
        self.policy = policy
        self.protocol = protocol
        self.message = message

    def check(self):
        raise RuntimeError(self.message)


class _BadResultGate:
    def __init__(self, policy, protocol, result):
        self.policy = policy
        self.protocol = protocol
        self.result = result

    def check(self):
        return self.result


def test_gate_exception_rejects_without_consuming_challenge(tmp_path):
    env = _setup(tmp_path)
    lifecycle = AuthorizationLifecycle(
        env["verifier"],
        env["policy"],
        env["protocol"],
        coordinated_gate=_RaisingGate(env["policy"], env["protocol"]),
    )

    result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.identity_verified is False
    assert result.execution_enabled is False
    assert result.reason == "Coordinated gate evaluation failed"
    assert "secret-token" not in result.reason
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"
    assert env["policy"].stored_state("auth-1") == "PENDING"


def test_invalid_gate_result_rejects(tmp_path):
    env = _setup(tmp_path)
    lifecycle = AuthorizationLifecycle(
        env["verifier"],
        env["policy"],
        env["protocol"],
        coordinated_gate=_BadResultGate(env["policy"], env["protocol"], {"allowed": True}),
    )

    result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.execution_enabled is False
    assert result.reason == "Coordinated gate result invalid"
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_component_mismatch_rejected(tmp_path):
    env = _setup(tmp_path)
    other_policy = AuthorizationPolicy(tmp_path / "other-auth.db")
    mismatched = CoordinatedHaltGate(env["coordinated"], other_policy, env["protocol"])

    with pytest.raises(ValueError, match="authorization policy mismatch"):
        AuthorizationLifecycle(
            env["verifier"],
            env["policy"],
            env["protocol"],
            coordinated_gate=mismatched,
        )

    other_protocol = CollaborationProtocol(tmp_path / "other-bridge.db")
    mismatched_protocol = CoordinatedHaltGate(env["coordinated"], env["policy"], other_protocol)
    with pytest.raises(ValueError, match="collaboration protocol mismatch"):
        AuthorizationLifecycle(
            env["verifier"],
            env["policy"],
            env["protocol"],
            coordinated_gate=mismatched_protocol,
        )


def test_aligned_components_accepted(tmp_path):
    env = _setup(tmp_path)
    assert env["lifecycle"].coordinated_gate.policy is env["policy"]
    assert env["lifecycle"].coordinated_gate.protocol is env["protocol"]
    result = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is True
    assert result.execution_enabled is False


def test_default_construction_unchanged(tmp_path):
    env = _setup(tmp_path)
    lifecycle = AuthorizationLifecycle(env["verifier"], env["policy"], env["protocol"])
    assert lifecycle.coordinated_gate is None
    result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is True
    assert result.execution_enabled is False


def test_lifecycle_result_execution_disabled_cannot_be_overridden():
    from skippy.collaboration.authorization_lifecycle import LifecycleResult

    result = LifecycleResult(False, "rejected", False, None)
    assert result.execution_enabled is False
    with pytest.raises(TypeError):
        LifecycleResult(False, "rejected", False, None, execution_enabled=True)
    with pytest.raises(Exception):
        result.execution_enabled = True
    assert result.execution_enabled is False


def test_all_outcomes_report_execution_disabled(tmp_path):
    env = _setup(tmp_path)
    accepted = env["lifecycle"].evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert accepted.accepted is True
    assert accepted.execution_enabled is False

    halted_dir = tmp_path / "halted"
    halted_dir.mkdir()
    halted = _setup(halted_dir, coordinated_state="HALTED")
    rejected = halted["lifecycle"].evaluate(
        halted["challenge"], _sign(halted["private"], halted["challenge"])
    )
    assert rejected.accepted is False
    assert rejected.execution_enabled is False


def test_allowed_result_reporting_halted_is_rejected(tmp_path):
    env = _setup(tmp_path)
    inconsistent = GateResult(True, "unsafe", "HALTED", True, False, False)
    lifecycle = AuthorizationLifecycle(
        env["verifier"],
        env["policy"],
        env["protocol"],
        coordinated_gate=_BadResultGate(env["policy"], env["protocol"], inconsistent),
    )
    result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.execution_enabled is False
    assert result.reason == "Coordinated gate result invalid"
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_allowed_result_with_incomplete_migration_is_rejected(tmp_path):
    env = _setup(tmp_path)
    inconsistent = GateResult(True, "unsafe", "NOT_HALTED", False, False, False)
    lifecycle = AuthorizationLifecycle(
        env["verifier"],
        env["policy"],
        env["protocol"],
        coordinated_gate=_BadResultGate(env["policy"], env["protocol"], inconsistent),
    )
    result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.execution_enabled is False
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_allowed_result_reporting_legacy_halt_is_rejected(tmp_path):
    env = _setup(tmp_path)
    policy_halted = GateResult(True, "unsafe", "NOT_HALTED", True, True, False)
    protocol_halted = GateResult(True, "unsafe", "NOT_HALTED", True, False, True)

    for inconsistent in (policy_halted, protocol_halted):
        lifecycle = AuthorizationLifecycle(
            env["verifier"],
            env["policy"],
            env["protocol"],
            coordinated_gate=_BadResultGate(env["policy"], env["protocol"], inconsistent),
        )
        result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
        assert result.accepted is False
        assert result.execution_enabled is False
        assert result.reason == "Coordinated gate result invalid"
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"


def test_correctly_formed_safe_result_is_accepted(tmp_path):
    env = _setup(tmp_path)
    safe = GateResult(
        True,
        "Safety conditions satisfied; execution disabled",
        "NOT_HALTED",
        True,
        False,
        False,
    )
    lifecycle = AuthorizationLifecycle(
        env["verifier"],
        env["policy"],
        env["protocol"],
        coordinated_gate=_BadResultGate(env["policy"], env["protocol"], safe),
    )
    result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is True
    assert result.identity_verified is True
    assert result.execution_enabled is False
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "consumed"


class _ExplosiveStatus(GateResult):
    def __getattribute__(self, name):
        if name == "coordinated_status":
            raise RuntimeError("secret-validation-token")
        return super().__getattribute__(name)


def test_result_validation_exception_fails_closed(tmp_path):
    env = _setup(tmp_path)
    explosive = _ExplosiveStatus(True, "unsafe", "NOT_HALTED", True, False, False)
    lifecycle = AuthorizationLifecycle(
        env["verifier"],
        env["policy"],
        env["protocol"],
        coordinated_gate=_BadResultGate(env["policy"], env["protocol"], explosive),
    )
    result = lifecycle.evaluate(env["challenge"], _sign(env["private"], env["challenge"]))
    assert result.accepted is False
    assert result.execution_enabled is False
    assert result.reason == "Coordinated gate evaluation failed"
    assert "secret-validation-token" not in result.reason
    assert _challenge_status(env["verifier"], env["challenge"]["challenge_id"]) == "open"
    assert env["policy"].stored_state("auth-1") == "PENDING"
