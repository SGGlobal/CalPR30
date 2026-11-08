"""
SKIPPY AI Collaboration Bridge — NIGHTSHIFT-01 Tests.

Synthetic. Offline. Non-executing.
"""

from datetime import datetime, timedelta, timezone
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
    assert report.state != "CORRECTION_REQUESTED"
    assert "PR authorization" in report.pending_chief_decisions


def test_successful_correction_aligns_ledger(tmp_path):
    """Correction cycle records submission and final review; never leaves CORRECTION_REQUESTED."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    report = runner.run(
        assignment_id="ns-corr",
        project_id="QLA",
        task_id="nightshift-corr",
        checkpoint_id="cp-corr",
        objective="needs correction",
        force_inadequate=True,
    )
    assert report.outcome == "synthetic-complete"
    assert report.correction_count == 1
    assert report.state != "CORRECTION_REQUESTED"
    assert report.source_verified is True
    assert report.execution_enabled is False


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


def test_mismatched_guard_rejected(tmp_path):
    """Guard not bound to protocol is rejected before assignment."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    # Create a different gate that is not bound
    other_protocol = CollaborationProtocol(tmp_path / "other.db")
    other_gate = CoordinatedHaltGate(tmp_path / "coord.db", policy, other_protocol)
    runner = NightshiftRunner(protocol, ledger, other_gate)
    report = runner.run(
        assignment_id="ns-mismatch",
        project_id="QLA",
        task_id="nightshift-mismatch",
        checkpoint_id="cp-mismatch",
        objective="should refuse",
    )
    assert report.outcome == "fail-closed"
    assert "Mismatched" in (report.refusal_reason or "") or "guard" in (report.refusal_reason or "").lower()


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


def test_restart_rehydrates_successful_report(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    original = runner.run(
        assignment_id="ns-004",
        project_id="QLA",
        task_id="nightshift-4",
        checkpoint_id="cp-4",
        objective="restart test",
    )
    # New runner on same ledger reconstructs without re-running
    reopened = NightshiftRunner(protocol, ledger, gate)
    reconstructed = reopened.report("ns-004")
    assert reconstructed is not None
    assert reconstructed.assignment_id == original.assignment_id
    assert reconstructed.outcome == original.outcome
    assert reconstructed.state == original.state
    assert reconstructed.correction_count == original.correction_count
    assert reconstructed.synthetic is True
    assert reconstructed.execution_enabled is False


def test_restart_rehydrates_refused_report(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    runner = NightshiftRunner(protocol, ledger, gate=None)
    original = runner.run(
        assignment_id="ns-refused",
        project_id="QLA",
        task_id="nightshift-refused",
        checkpoint_id="cp-refused",
        objective="refuse",
    )
    reopened = NightshiftRunner(protocol, ledger, gate=None)
    reconstructed = reopened.report("ns-refused")
    assert reconstructed is not None
    assert reconstructed.outcome == "fail-closed"
    assert reconstructed.refusal_reason == original.refusal_reason


def test_step_budget_exhausted(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate, max_steps=1)
    report = runner.run(
        assignment_id="ns-budget",
        project_id="QLA",
        task_id="nightshift-budget",
        checkpoint_id="cp-budget",
        objective="budget",
    )
    assert report.outcome == "fail-closed"
    assert "Step budget" in (report.refusal_reason or "")


def test_timeout_exceeded(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    start = datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc)
    times = [start, start + timedelta(seconds=100)]
    clock = lambda: times.pop(0) if times else start + timedelta(seconds=200)
    runner = NightshiftRunner(protocol, ledger, gate, clock=clock, timeout_seconds=1)
    report = runner.run(
        assignment_id="ns-timeout",
        project_id="QLA",
        task_id="nightshift-timeout",
        checkpoint_id="cp-timeout",
        objective="timeout",
    )
    assert report.outcome == "fail-closed"
    assert "Timeout" in (report.refusal_reason or "")


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
