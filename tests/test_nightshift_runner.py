"""
SKIPPY AI Collaboration Bridge — NIGHTSHIFT-01 Tests.

Synthetic. Offline. Non-executing.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from skippy.collaboration.authorization import AuthorizationPolicy
from skippy.collaboration.checkpoint_ledger import CheckpointLedger
from skippy.collaboration.coordinated_halt_gate import CoordinatedHaltGate
from skippy.collaboration.nightshift_runner import NightshiftRunner
from skippy.collaboration.protocol import CollaborationProtocol


def _setup(tmp_path, halted=False):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    coordinated = tmp_path / "coord.db"
    # Minimal coordinated authority fixture
    import sqlite3
    with sqlite3.connect(coordinated) as c:
        c.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("""
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        c.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
        state = "HALTED" if halted else "NOT_HALTED"
        seq = 1 if halted else 0
        c.execute(
            "INSERT INTO halt_state VALUES (1, ?, ?, '2026-01-01T00:00:00+00:00', 'test')",
            (state, seq),
        )
    gate = CoordinatedHaltGate(coordinated, policy, protocol)
    policy.require_coordinated_guard(gate)
    protocol.require_coordinated_guard(gate)
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    return protocol, policy, gate, ledger


def test_golden_path_synthetic_handoff(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    report = runner.run(
        assignment_id="ns-001",
        project_id="QLA",
        task_id="nightshift-1",
        checkpoint_id="cp-1",
        objective="synthetic bounded handoff",
    )
    assert report.synthetic is True
    assert report.execution_enabled is False
    assert report.outcome == "synthetic-complete"
    assert report.ci_verified is False
    assert report.refusal_reason is None
    assert "PR authorization" in report.pending_chief_decisions


def test_missing_guard_refuses(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    runner = NightshiftRunner(protocol, ledger, gate=None)
    report = runner.run(
        assignment_id="ns-002",
        project_id="QLA",
        task_id="nightshift-2",
        checkpoint_id="cp-2",
        objective="should refuse",
    )
    assert report.outcome == "fail-closed"
    assert "guard" in (report.refusal_reason or "").lower()
    assert report.execution_enabled is False


def test_halted_guard_refuses(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path, halted=True)
    runner = NightshiftRunner(protocol, ledger, gate)
    report = runner.run(
        assignment_id="ns-003",
        project_id="QLA",
        task_id="nightshift-3",
        checkpoint_id="cp-3",
        objective="should refuse on halt",
    )
    assert report.outcome == "fail-closed"
    assert report.execution_enabled is False


def test_restart_rehydrates_report(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    runner.run(
        assignment_id="ns-004",
        project_id="QLA",
        task_id="nightshift-4",
        checkpoint_id="cp-4",
        objective="restart test",
    )
    # New runner on same ledger
    reopened = NightshiftRunner(protocol, ledger, gate)
    reconstructed = reopened.report("QLA", "nightshift-4")
    assert reconstructed is not None
    assert reconstructed.synthetic is True
    assert reconstructed.execution_enabled is False
    assert reconstructed.ci_verified is False


def test_duplicate_runner_refuses(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    runner.run(
        assignment_id="ns-005",
        project_id="QLA",
        task_id="nightshift-5",
        checkpoint_id="cp-5",
        objective="first",
    )
    second = runner.run(
        assignment_id="ns-006",
        project_id="QLA",
        task_id="nightshift-6",
        checkpoint_id="cp-6",
        objective="second",
    )
    assert second.outcome == "fail-closed"
    assert "Duplicate" in (second.refusal_reason or "")
