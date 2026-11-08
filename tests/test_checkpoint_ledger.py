"""
SKIPPY AI Collaboration Bridge — Checkpoint Ledger Tests.

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


def test_acceptance_scenario_step3_then_correction_then_duplicate(tmp_path):
    """
    Acceptance scenario from Issue #20 / BRIDGE-01:

    - PR #19 merged with 641 independently CI-verified tests (modeled as prior event).
    - Step 69E.3B.2F.3 implementation reported at f9021af with 664 locally reported tests.
    - Independent review requests Step 3A corrections.
    - Re-sending the same Step 3 report must not overwrite CORRECTION_REQUESTED.
    """
    ledger = CheckpointLedger(tmp_path / "ledger.db")

    # Prior merged PR #19 with CI verification (synthetic, separate task for isolation)
    ledger.record_event(
        event_type=EventType.CI_VERIFICATION_RECORDED,
        project_id="QLA",
        task_id="pr19-merged",
        checkpoint_id="pr19",
        actor="ci",
        payload={"sha": "aa0bfca", "ci_tests": 641, "verified": True},
        message_id="msg-ci-pr19",
        idempotency_key="idem-ci-pr19",
    )
    ledger.record_event(
        event_type=EventType.MERGED,
        project_id="QLA",
        task_id="pr19-merged",
        checkpoint_id="pr19",
        actor="chief",
        payload={"sha": "aa0bfca"},
        message_id="msg-merged-pr19",
        idempotency_key="idem-merged-pr19",
    )

    # Step 3 implementation reported (local tests only) — fresh checkpoint
    rev = ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="QLA",
        task_id="phase9-step3",
        checkpoint_id="step3",
        actor="jason",
        payload={"sha": "f9021af9857e6e72809a683bf5260561ba4e4b61", "local_tests": 664},
        message_id="msg-step3-impl",
        idempotency_key="idem-step3-impl",
        expected_revision=2,
    )
    assert rev == 3

    status = ledger.status("QLA", "phase9-step3")
    assert status.state == CheckpointState.IMPLEMENTED_REPORTED
    assert status.local_tests_reported == 664
    assert status.ci_verified is False
    assert status.current_sha.startswith("f9021af")

    # Review requests correction
    ledger.record_event(
        event_type=EventType.CORRECTION_REQUESTED,
        project_id="QLA",
        task_id="phase9-step3",
        checkpoint_id="step3",
        actor="skippy",
        payload={"findings": "Step 3A corrections requested", "sha": "f9021af..."},
        message_id="msg-correction-req",
        idempotency_key="idem-correction-req",
        expected_revision=3,
    )

    status = ledger.status("QLA", "phase9-step3")
    assert status.state == CheckpointState.CORRECTION_REQUESTED
    assert "3A" in (status.outstanding_findings or "")

    # Re-send identical Step 3 report — must be rejected as duplicate/stale
    with pytest.raises(LedgerError, match="Duplicate"):
        ledger.record_event(
            event_type=EventType.IMPLEMENTATION_SUBMITTED,
            project_id="QLA",
            task_id="phase9-step3",
            checkpoint_id="step3",
            actor="jason",
            payload={"sha": "f9021af9857e6e72809a683bf5260561ba4e4b61", "local_tests": 664},
            message_id="msg-step3-impl",  # same message_id
            idempotency_key="idem-step3-impl",
        )

    # State unchanged
    status_after = ledger.status("QLA", "phase9-step3")
    assert status_after.state == CheckpointState.CORRECTION_REQUESTED
    assert status_after.revision == 4


def test_duplicate_idempotency_key_rejects(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
        idempotency_key="same-key",
    )
    with pytest.raises(LedgerError, match="Duplicate idempotency_key"):
        ledger.record_event(
            event_type=EventType.IMPLEMENTATION_SUBMITTED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="jason",
            payload={"sha": "abc"},
            idempotency_key="same-key",
        )


def test_stale_expected_revision_rejects(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
    )
    with pytest.raises(LedgerError, match="Stale or concurrent revision"):
        ledger.record_event(
            event_type=EventType.REVIEW_RECORDED,
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="skippy",
            payload={},
            expected_revision=0,  # already advanced
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
    )
    status = ledger.status("p", "t")
    assert status.local_tests_reported == 664
    assert status.ci_verified is False
    assert status.ci_tests is None


def test_pr_and_merge_authorizations_distinct(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.PR_AUTHORIZED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"authorized": True},
    )
    status = ledger.status("p", "t")
    assert status.pr_authorized is True
    assert status.merge_authorized is False

    ledger.record_event(
        event_type=EventType.MERGE_AUTHORIZED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"authorized": True},
    )
    status = ledger.status("p", "t")
    assert status.pr_authorized is True
    assert status.merge_authorized is True


def test_restart_rebuilds_identical_status(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = CheckpointLedger(path)
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc", "local_tests": 100},
    )
    ledger.record_event(
        event_type=EventType.CORRECTION_REQUESTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="skippy",
        payload={"findings": "fix it"},
    )

    # New instance on same DB
    reopened = CheckpointLedger(path)
    status = reopened.status("p", "t")
    assert status.state == CheckpointState.CORRECTION_REQUESTED
    assert status.outstanding_findings == "fix it"
    assert status.revision == 2


def test_malformed_rejects_without_effects(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    with pytest.raises(LedgerError):
        ledger.record_event(
            event_type="NOT_AN_ENUM",  # type: ignore
            project_id="p",
            task_id="t",
            checkpoint_id="c1",
            actor="jason",
            payload={},
        )
    # No events recorded
    assert ledger.status("p", "t") is None


def test_halt_sets_halted_state(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    ledger.record_event(
        event_type=EventType.IMPLEMENTATION_SUBMITTED,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="jason",
        payload={"sha": "abc"},
    )
    ledger.record_event(
        event_type=EventType.HALT,
        project_id="p",
        task_id="t",
        checkpoint_id="c1",
        actor="chief",
        payload={"reason": "manual"},
    )
    status = ledger.status("p", "t")
    assert status.state == CheckpointState.HALTED
