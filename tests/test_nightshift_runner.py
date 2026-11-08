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
    assert report.outcome == "uncertain"
    assert "Step budget" in (report.refusal_reason or "")


def test_timeout_exceeded(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    mono_times = [1000.0, 1000.0, 1100.0]
    mono = lambda: mono_times.pop(0) if mono_times else 1200.0
    runner = NightshiftRunner(protocol, ledger, gate, mono=mono, timeout_seconds=1)
    report = runner.run(
        assignment_id="ns-timeout",
        project_id="QLA",
        task_id="nightshift-timeout",
        checkpoint_id="cp-timeout",
        objective="timeout",
    )
    assert report.outcome == "uncertain"
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


def test_duplicate_submission_preserves_original_report(tmp_path):
    """Successful report remains unchanged after duplicate submission, including after restart."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    original = runner.run(
        assignment_id="ns-preserve",
        project_id="QLA",
        task_id="nightshift-preserve",
        checkpoint_id="cp-preserve",
        objective="preserve",
    )
    assert original.outcome == "synthetic-complete"

    # Duplicate submission on same runner raises (original preserved)
    with pytest.raises(Exception):
        runner.run(
            assignment_id="ns-preserve",
            project_id="QLA",
            task_id="nightshift-preserve",
            checkpoint_id="cp-preserve",
            objective="preserve-again",
        )
    # Original persisted report must be unchanged
    reopened = NightshiftRunner(protocol, ledger, gate)
    reconstructed = reopened.report("ns-preserve")
    assert reconstructed is not None
    assert reconstructed.outcome == original.outcome
    assert reconstructed.state == original.state
    assert reconstructed.correction_count == original.correction_count
    assert reconstructed.refusal_reason == original.refusal_reason


def test_oversized_step_limit_rejected(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    with pytest.raises(Exception, match="Step limit"):
        NightshiftRunner(protocol, ledger, gate, max_steps=1000)


def test_oversized_timeout_rejected(tmp_path):
    protocol, policy, gate, ledger = _setup(tmp_path)
    with pytest.raises(Exception, match="Timeout"):
        NightshiftRunner(protocol, ledger, gate, timeout_seconds=10000)


def test_assignment_identity_after_restart(tmp_path):
    """Identical redelivery after restart does not dispatch new work or alter the report."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    original = runner.run(
        assignment_id="ns-identity",
        project_id="QLA",
        task_id="nightshift-id",
        checkpoint_id="cp-id",
        objective="identity",
    )
    assert original.outcome == "synthetic-complete"

    # Newly constructed runner after restart
    reopened = NightshiftRunner(protocol, ledger, gate)
    redelivery = reopened.run(
        assignment_id="ns-identity",
        project_id="QLA",
        task_id="nightshift-id",
        checkpoint_id="cp-id",
        objective="identity",
    )
    assert redelivery.outcome == original.outcome
    assert redelivery.state == original.state
    assert redelivery.correction_count == original.correction_count

    # Conflicting reuse rejected
    with pytest.raises(Exception, match="Conflicting"):
        reopened.run(
            assignment_id="ns-identity",
            project_id="OTHER",
            task_id="nightshift-id",
            checkpoint_id="cp-id",
            objective="conflict",
        )


def test_monotonic_timeout(tmp_path):
    """Injected monotonic clock enforces timeout."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    mono_times = [1000.0, 1000.0, 1100.0]
    mono = lambda: mono_times.pop(0) if mono_times else 1200.0
    runner = NightshiftRunner(protocol, ledger, gate, mono=mono, timeout_seconds=1)
    report = runner.run(
        assignment_id="ns-mono",
        project_id="QLA",
        task_id="nightshift-mono",
        checkpoint_id="cp-mono",
        objective="mono",
    )
    assert report.outcome == "uncertain"
    assert "Timeout" in (report.refusal_reason or "")


def test_wall_clock_change_cannot_bypass_timeout(tmp_path):
    """Wall-clock changes do not affect monotonic timeout enforcement."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    # Wall clock jumps backward; monotonic clock advances
    wall_times = [
        datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc),  # backward
    ]
    mono_times = [1000.0, 1000.0, 1100.0]
    clock = lambda: wall_times.pop(0) if wall_times else datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
    mono = lambda: mono_times.pop(0) if mono_times else 1200.0
    runner = NightshiftRunner(protocol, ledger, gate, clock=clock, mono=mono, timeout_seconds=1)
    report = runner.run(
        assignment_id="ns-wall",
        project_id="QLA",
        task_id="nightshift-wall",
        checkpoint_id="cp-wall",
        objective="wall",
    )
    assert report.outcome == "uncertain"
    assert "Timeout" in (report.refusal_reason or "")


def test_conflicting_objective_rejected(tmp_path):
    """Conflicting objective with same assignment ID fails closed and preserves original."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)
    original = runner.run(
        assignment_id="ns-obj",
        project_id="QLA",
        task_id="nightshift-obj",
        checkpoint_id="cp-obj",
        objective="original",
    )
    reopened = NightshiftRunner(protocol, ledger, gate)
    with pytest.raises(Exception, match="Conflicting"):
        reopened.run(
            assignment_id="ns-obj",
            project_id="QLA",
            task_id="nightshift-obj",
            checkpoint_id="cp-obj",
            objective="different",
        )
    reconstructed = reopened.report("ns-obj")
    assert reconstructed.objective == "original"
    assert reconstructed.outcome == original.outcome


def test_refused_report_preserves_identity(tmp_path):
    """Refused reports persist objective; identical redelivery after restart preserves it."""
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    runner = NightshiftRunner(protocol, ledger, gate=None)
    original = runner.run(
        assignment_id="ns-refuse-id",
        project_id="QLA",
        task_id="nightshift-refuse-id",
        checkpoint_id="cp-refuse-id",
        objective="original-objective",
        force_inadequate=True,
    )
    assert original.outcome == "fail-closed"
    assert original.objective == "original-objective"
    assert original.force_inadequate is True

    # Restart and identical redelivery
    reopened = NightshiftRunner(protocol, ledger, gate=None)
    redelivery = reopened.run(
        assignment_id="ns-refuse-id",
        project_id="QLA",
        task_id="nightshift-refuse-id",
        checkpoint_id="cp-refuse-id",
        objective="original-objective",
        force_inadequate=True,
    )
    assert redelivery.outcome == original.outcome
    assert redelivery.objective == original.objective

    # Conflicting redelivery rejected
    with pytest.raises(Exception, match="Conflicting"):
        reopened.run(
            assignment_id="ns-refuse-id",
            project_id="QLA",
            task_id="nightshift-refuse-id",
            checkpoint_id="cp-refuse-id",
            objective="different-objective",
        )


def test_missing_approval_is_uncertain(tmp_path, monkeypatch):
    """Missing or non-approved outcome is UNCERTAIN."""
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)

    def fake_run(task_id, objective, force_correction=False):
        return {
            "task_id": task_id,
            "status": "COMPLETE",
            "outcome": None,  # missing
            "corrections": 0,
            "unacknowledged": [],
            "halted": False,
        }

    monkeypatch.setattr(runner.orchestrator, "run", fake_run)
    report = runner.run(
        assignment_id="ns-no-approval",
        project_id="QLA",
        task_id="nightshift-no-approval",
        checkpoint_id="cp-no-approval",
        objective="no approval",
    )
    assert report.outcome == "uncertain"
    assert "not approved" in (report.refusal_reason or "")


def test_rejected_approval_is_uncertain(tmp_path, monkeypatch):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)

    def fake_run(task_id, objective, force_correction=False):
        return {
            "task_id": task_id,
            "status": "COMPLETE",
            "outcome": "rejected",
            "corrections": 0,
            "unacknowledged": [],
            "halted": False,
        }

    monkeypatch.setattr(runner.orchestrator, "run", fake_run)
    report = runner.run(
        assignment_id="ns-rejected",
        project_id="QLA",
        task_id="nightshift-rejected",
        checkpoint_id="cp-rejected",
        objective="rejected",
    )
    assert report.outcome == "uncertain"
    assert "not approved" in (report.refusal_reason or "")


def test_mismatched_correction_count_is_uncertain(tmp_path, monkeypatch):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)

    def fake_run(task_id, objective, force_correction=False):
        return {
            "task_id": task_id,
            "status": "COMPLETE",
            "outcome": "approved",
            "corrections": 5,  # mismatch
            "unacknowledged": [],
            "halted": False,
        }

    monkeypatch.setattr(runner.orchestrator, "run", fake_run)
    report = runner.run(
        assignment_id="ns-mismatch-corr",
        project_id="QLA",
        task_id="nightshift-mismatch-corr",
        checkpoint_id="cp-mismatch-corr",
        objective="mismatch",
    )
    assert report.outcome == "uncertain"
    assert "Correction count mismatch" in (report.refusal_reason or "")


def test_outstanding_acknowledgments_is_uncertain(tmp_path, monkeypatch):
    protocol, policy, gate, ledger = _setup(tmp_path)
    runner = NightshiftRunner(protocol, ledger, gate)

    def fake_run(task_id, objective, force_correction=False):
        return {
            "task_id": task_id,
            "status": "COMPLETE",
            "outcome": "approved",
            "corrections": 0,
            "unacknowledged": ["msg-1"],
            "halted": False,
        }

    monkeypatch.setattr(runner.orchestrator, "run", fake_run)
    report = runner.run(
        assignment_id="ns-unacked",
        project_id="QLA",
        task_id="nightshift-unacked",
        checkpoint_id="cp-unacked",
        objective="unacked",
    )
    assert report.outcome == "uncertain"
    assert "Outstanding acknowledgments" in (report.refusal_reason or "")
