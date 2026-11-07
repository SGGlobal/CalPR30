"""
SKIPPY AI Collaboration Bridge — system-wide HALT coverage facts.

These tests record current behavior. They do not add enforcement.
Synthetic. Offline. Non-executing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from skippy.collaboration.coordinated_halt_gate import CoordinatedHaltGate
from skippy.collaboration.orchestrator import CollaborationOrchestrator
from skippy.collaboration.protocol import (
    CollaborationProtocol,
    Message,
    MessageType,
    ProtocolError,
)


def _init_authority(path: Path, state: str = "HALTED", sequence: int = 1) -> None:
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


def _message():
    return Message(
        message_id="msg-coverage-1",
        idempotency_key="coverage-1",
        correlation_id="corr-coverage",
        project_id="QLA",
        task_id="T-coverage",
        sender="skyppy",
        recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT,
        timestamp=datetime.now(timezone.utc),
    )


def test_ungated_protocol_accepts_while_coordinated_is_halted(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.coordinated_guard is None
    assert protocol.accept(_message()) is True
    assert protocol.get_state("T-coverage").value == "ASSIGNED"
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_gated_protocol_rejects_the_same_condition(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    protocol.require_coordinated_guard(
        CoordinatedHaltGate(coordinated, policy, protocol)
    )
    try:
        protocol.accept(_message())
        assert False, "gated protocol accepted work while coordinated was HALTED"
    except ProtocolError as exc:
        assert "Coordinated guard rejected" in str(exc)
    assert protocol.get_state("T-coverage") is None


def test_ungated_orchestrator_runs_while_coordinated_is_halted(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)
    summary = orchestrator.run("T-orch", "coverage objective")
    assert summary["status"] == "COMPLETE"
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED


def test_ungated_lifecycle_evaluates_while_coordinated_is_halted(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    verifier.register_public_key("test-key", public)
    created = datetime.now(timezone.utc) - timedelta(minutes=1)
    policy.submit(
        AuthorizationRequest(
            authorization_id="auth-coverage",
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
        authorization_id="auth-coverage",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        key_id="test-key",
    )
    lifecycle = AuthorizationLifecycle(verifier, policy, protocol)
    assert lifecycle.coordinated_gate is None
    result = lifecycle.evaluate(challenge, private.sign(canonical_payload(challenge)))
    assert result.accepted is True
    assert result.execution_enabled is False
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
