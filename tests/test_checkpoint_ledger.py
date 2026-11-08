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
        payload={"sha": "abc123", "source_verified": True},
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
    """Finding 4 / BRIDGE-01B: structured, separately reviewed evidence required."""
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
    # Truthy but unstructured
    with pytest.raises(LedgerError, match="Structured authorization evidence"):
        ledger.record_event(
            event_type=EventType.PR_AUTHORIZED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="chief",
            payload={"authorization_evidence": True},
            expected_revision=2,
        )
    # Structured but not reviewed
    with pytest.raises(LedgerError, match="not separately reviewed"):
        ledger.record_event(
            event_type=EventType.PR_AUTHORIZED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="chief",
            payload={"authorization_evidence": {"authorization_id": "auth-1"}},
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


def test_conflicting_message_id_across_projects_rejected(tmp_path):
    """BRIDGE-01B Task 1: complete envelope identity."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p1",
        task_id="t1",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        message_id="shared-msg",
        expected_revision=0,
    )
    with pytest.raises(LedgerError, match="Conflicting reuse"):
        ledger.record_event(
            event_type=EventType.IMPLEMENTATION_SUBMITTED,
            project_id="p2",  # different project
            task_id="t1",
            checkpoint_id="c1",
            actor="jason",
            payload={"sha": "abc"},
            message_id="shared-msg",
            expected_revision=1,
        )


def test_review_must_reference_current_sha(tmp_path):
    """BRIDGE-01B Task 2: review SHA must match current implementation."""
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
    with pytest.raises(LedgerError, match="Review SHA does not match"):
        ledger.record_event(
            event_type=EventType.REVIEW_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="skippy",
            payload={"sha": "wrongsha"},
            expected_revision=1,
        )


def test_ci_requires_prior_review(tmp_path):
    """BRIDGE-01B Task 2: CI before source review rejected."""
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
    with pytest.raises(LedgerError, match="requires prior source review"):
        ledger.record_event(
            event_type=EventType.CI_VERIFICATION_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="ci",
            payload={
                "sha": "abc",
                "workflow_run_id": "run-1",
                "conclusion": "success",
                "tests": 641,
            },
            expected_revision=1,
        )


def test_new_sha_invalidates_prior_ci(tmp_path):
    """BRIDGE-01B Task 2: new SHA clears prior CI validity."""
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
    ledger.record_event(
        event_type=EventType.CI_VERIFICATION_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="ci",
        payload={
            "sha": "abc",
            "workflow_run_id": "run-1",
            "conclusion": "success",
            "tests": 641,
        },
        expected_revision=2,
    )
    status = ledger.status("p", "t")
    assert status.ci_verified is True

    # New correction changes SHA
    ledger.record_event(
        event_type=EventType.CORRECTION_REQUESTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"findings": "update"},
        expected_revision=3,
    )
    ledger.record_event(
        event_type=EventType.CORRECTION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "def456", "local_tests": 650},
        expected_revision=4,
    )
    status = ledger.status("p", "t")
    assert status.current_sha == "def456"
    assert status.ci_verified is False
    assert status.ci_sha is None


def test_inconsistent_checkpoint_id_rejected(tmp_path):
    """BRIDGE-01B Task 4: consistent checkpoint identifiers."""
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
    with pytest.raises(LedgerError, match="Inconsistent checkpoint identifier"):
        ledger.record_event(
            event_type=EventType.REVIEW_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c2",  # different
            actor="skippy",
            payload={"sha": "abc"},
            expected_revision=1,
        )


def test_legal_progression_to_merged(tmp_path):
    """BRIDGE-01B Task 4: reachable transitions through full progression."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    # Implementation
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        expected_revision=0,
    )
    # Review
    ledger.record_event(
        event_type=EventType.REVIEW_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"sha": "abc", "source_verified": True},
        expected_revision=1,
    )
    # CI
    ledger.record_event(
        event_type=EventType.CI_VERIFICATION_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="ci",
        payload={
            "sha": "abc",
            "workflow_run_id": "run-1",
            "conclusion": "success",
            "tests": 641,
        },
        expected_revision=2,
    )
    # PR authorized
    ledger.record_event(
        event_type=EventType.PR_AUTHORIZED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={
            "authorization_evidence": {
                "authorization_id": "auth-pr",
                "reviewed": True,
            }
        },
        expected_revision=3,
    )
    # PR opened
    ledger.record_event(
        event_type=EventType.PR_OPENED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"pr_number": 21},
        expected_revision=4,
    )
    # Merge authorized
    ledger.record_event(
        event_type=EventType.MERGE_AUTHORIZED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={
            "authorization_evidence": {
                "authorization_id": "auth-merge",
                "reviewed": True,
            }
        },
        expected_revision=5,
    )
    # Merged
    ledger.record_event(
        event_type=EventType.MERGED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"sha": "abc"},
        expected_revision=6,
    )
    status = ledger.status("p", "t")
    assert status.state == CheckpointState.MERGED
    assert status.pr_authorized is True
    assert status.merge_authorized is True
    assert status.ci_verified is True


def test_both_identifiers_must_match_same_event(tmp_path):
    """BRIDGE-01C: mixed identifiers produce pair-mismatch rejection."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    # Two events with identical envelope content except message/idempotency pair
    # Use HALT which is always allowed so both can be recorded
    ledger.record_event(
        event_type=EventType.HALT,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"reason": "test"},
        message_id="msg-a",
        idempotency_key="idem-a",
    )
    ledger.record_event(
        event_type=EventType.HALT,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"reason": "test"},
        message_id="msg-b",
        idempotency_key="idem-b",
    )
    # Mix first message_id with second idempotency_key — must hit pair comparison
    with pytest.raises(LedgerError, match="pair mismatch|Conflicting identity"):
        ledger.record_event(
            event_type=EventType.HALT,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="chief",
            payload={"reason": "test"},
            message_id="msg-a",
            idempotency_key="idem-b",
        )
    # Exact duplicate of first event remains no-op
    rev = ledger.record_event(
        event_type=EventType.HALT,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"reason": "test"},
        message_id="msg-a",
        idempotency_key="idem-a",
    )
    assert rev == 2  # unchanged


def test_ci_requires_source_verified_review(tmp_path):
    """BRIDGE-01C Task 2: CI bound to explicitly source-verified current SHA."""
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
    # Review without source_verified
    ledger.record_event(
        event_type=EventType.REVIEW_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"sha": "abc"},  # no source_verified
        expected_revision=1,
    )
    with pytest.raises(LedgerError, match="explicitly source-verified"):
        ledger.record_event(
            event_type=EventType.CI_VERIFICATION_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="ci",
            payload={
                "sha": "abc",
                "workflow_run_id": "run-1",
                "conclusion": "success",
                "tests": 641,
            },
            expected_revision=2,
        )

    # Now record a source-verified review
    ledger.record_event(
        event_type=EventType.REVIEW_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"sha": "abc", "source_verified": True},
        expected_revision=2,
    )
    # Matching CI succeeds
    ledger.record_event(
        event_type=EventType.CI_VERIFICATION_RECORDED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="ci",
        payload={
            "sha": "abc",
            "workflow_run_id": "run-1",
            "conclusion": "success",
            "tests": 641,
        },
        expected_revision=3,
    )
    status = ledger.status("p", "t")
    assert status.ci_verified is True
    assert status.ci_sha == "abc"

    # New correction changes SHA
    ledger.record_event(
        event_type=EventType.CORRECTION_REQUESTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"findings": "update"},
        expected_revision=4,
    )
    ledger.record_event(
        event_type=EventType.CORRECTION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "def999"},
        expected_revision=5,
    )
    # Old CI SHA rejected for new revision
    with pytest.raises(LedgerError, match="Reviewed SHA does not match|does not match verified current source"):
        ledger.record_event(
            event_type=EventType.CI_VERIFICATION_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="ci",
            payload={
                "sha": "abc",  # old SHA
                "workflow_run_id": "run-2",
                "conclusion": "success",
                "tests": 650,
            },
            expected_revision=6,
        )
    status = ledger.status("p", "t")
    assert status.current_sha == "def999"
    assert status.ci_verified is False


def test_assignment_created_not_uncertain(tmp_path):
    """Assignment is represented and does not appear as UNCERTAIN."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.ASSIGNMENT_CREATED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"objective": "test"},
        expected_revision=0,
    )
    status = ledger.status("p", "t")
    assert status is not None
    assert status.state != CheckpointState.UNCERTAIN
    assert status.checkpoint_id == "c1"


def test_conflicting_assignment_rejected(tmp_path):
    """Multiple conflicting assignments under the same project/task are rejected."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.ASSIGNMENT_CREATED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"objective": "first"},
        expected_revision=0,
    )
    with pytest.raises(LedgerError, match="Conflicting assignment|Inconsistent checkpoint"):
        ledger.record_event(
            event_type=EventType.ASSIGNMENT_CREATED,
            project_id="p",
            task_id="t",
            checkpoint_id="c2",
            actor="chief",
            payload={"objective": "second"},
            expected_revision=1,
        )
    status = ledger.status("p", "t")
    assert status.checkpoint_id == "c1"
    assert status.revision == 1
