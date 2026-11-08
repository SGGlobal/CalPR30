"""
SKIPPY AI Collaboration Bridge — Checkpoint Ledger Tests (BRIDGE-01A).

Synthetic. Offline. Non-executing.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from skippy.collaboration.checkpoint_ledger import (
    CheckpointLedger,
    CheckpointState,
    EventType,
    LedgerError,
)


def _now():
    return datetime.now(timezone.utc)


def test_stale_implementation_does_not_overwrite_correction(tmp_path):
    """Finding 1: old report with new message IDs must not reset CORRECTION_REQUESTED."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")

    # Step 3 implementation
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="QLA",
        task_id="phase9-step3",
        checkpoint_id="step3",
        actor="jason",
        payload={"sha": "f9021af9857e6e72809a683bf5260561ba4e4b61", "local_tests": 664},
        message_id="msg-step3",
        idempotency_key="idem-step3",
        expected_revision=0,
    )

    # Correction requested
    ledger.record_event(
        event_type=EventType.CORRECTION_REQUESTED,
        project_id="QLA",
        task_id="phase9-step3",
        checkpoint_id="step3",
        actor="skippy",
        payload={"findings": "Step 3A corrections requested", "sha": "f9021af..."},
        message_id="msg-corr",
        idempotency_key="idem-corr",
        expected_revision=1,
    )

    status = ledger.status("QLA", "phase9-step3")
    assert status.state == CheckpointState.CORRECTION_REQUESTED

    # Original Step 3 report with entirely new message identifiers — must be stale
    with pytest.raises(LedgerError, match="Stale implementation report"):
        ledger.record_event(
            event_type=EventType.IMPLEMENTATION_SUBMITTED,
            project_id="QLA",
            task_id="phase9-step3",
            checkpoint_id="step3",
            actor="jason",
            payload={"sha": "f9021af9857e6e72809a683bf5260561ba4e4b61", "local_tests": 664},
            message_id="msg-step3-retry",
            idempotency_key="idem-step3-retry",
            expected_revision=2,
        )

    status_after = ledger.status("QLA", "phase9-step3")
    assert status_after.state == CheckpointState.CORRECTION_REQUESTED
    assert status_after.revision == 2


def test_illegal_transitions_rejected(tmp_path):
    """Finding 2: enforce state transitions."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        expected_revision=0,
    )
    # MERGED before merge authorization
    with pytest.raises(LedgerError, match="Illegal transition"):
        ledger.record_event(
            event_type=EventType.MERGED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="chief",
            payload={},
            expected_revision=1,
        )


def test_ci_evidence_must_match_reviewed_sha(tmp_path):
    """Finding 3: CI evidence bound to correct commit."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc123"},
        expected_revision=0,
    )
    ledger.record_event(
        event_type=EventType.REVIEW_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"sha": "abc123"},
        expected_revision=1,
    )
    # Incomplete evidence
    with pytest.raises(LedgerError, match="CI evidence incomplete"):
        ledger.record_event(
            event_type=EventType.CI_VERIFICATION_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="ci",
            payload={"sha": "abc123"},
            expected_revision=2,
        )
    # SHA mismatch
    with pytest.raises(LedgerError, match="CI evidence SHA does not match"):
        ledger.record_event(
            event_type=EventType.CI_VERIFICATION_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="ci",
            payload={
                "sha": "wrongsha",
                "workflow_run_id": "run-1",
                "conclusion": "success",
                "tests": 641,
            },
            expected_revision=2,
        )


def test_authorization_requires_explicit_evidence(tmp_path):
    """Finding 4: actor=chief is not sufficient."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        expected_revision=0,
    )
    ledger.record_event(
        event_type=EventType.REVIEW_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"sha": "abc", "source_verified": True},
        expected_revision=1,
    )
    with pytest.raises(LedgerError, match="authorization evidence required"):
        ledger.record_event(
            event_type=EventType.PR_AUTHORIZED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="chief",
            payload={},
            expected_revision=2,
        )


def test_ordinary_work_rejected_after_halt(tmp_path):
    """Finding 5: no automatic recovery from HALTED."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        expected_revision=0,
    )
    ledger.record_event(
        event_type=EventType.HALT,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"reason": "manual"},
    )
    with pytest.raises(LedgerError, match="Ordinary work rejected"):
        ledger.record_event(
            event_type=EventType.IMPLEMENTATION_SUBMITTED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="jason",
            payload={"sha": "def"},
            expected_revision=2,
        )
    status = ledger.status("p", "t")
    assert status.state == CheckpointState.HALTED


def test_expected_revision_required(tmp_path):
    """Finding 6: optimistic concurrency required."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    with pytest.raises(LedgerError, match="expected_revision required"):
        ledger.record_event(
            event_type=EventType.IMPLEMENTATION_SUBMITTED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="jason",
            payload={"sha": "abc"},
        )


def test_duplicate_identical_is_noop(tmp_path):
    """Finding 8: identical redelivery is no-op; conflicting reuse rejects."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    rev = ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        message_id="msg-1",
        expected_revision=0,
    )
    # Identical redelivery
    rev2 = ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        message_id="msg-1",
        expected_revision=1,
    )
    assert rev2 == rev  # no-op, same revision

    # Conflicting reuse
    with pytest.raises(LedgerError, match="Conflicting reuse"):
        ledger.record_event(
            event_type=EventType.IMPLEMENTATION_SUBMITTED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="jason",
            payload={"sha": "different"},
            message_id="msg-1",
            expected_revision=1,
        )


def test_local_tests_never_become_ci(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc", "local_tests": 664},
        expected_revision=0,
    )
    status = ledger.status("p", "t")
    assert status.local_tests_reported == 664
    assert status.ci_verified is False


def test_restart_rebuilds_status(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = CheckpointLedger(path)
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc", "local_tests": 100},
        expected_revision=0,
    )
    ledger.record_event(
        event_type=EventType.CORRECTION_REQUESTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"findings": "fix it"},
        expected_revision=1,
    )
    reopened = CheckpointLedger(path)
    status = reopened.status("p", "t")
    assert status.state == CheckpointState.CORRECTION_REQUESTED
    assert status.outstanding_findings == "fix it"
