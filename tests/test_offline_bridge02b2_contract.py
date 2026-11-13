"""Deterministic BRIDGE-02B.2.2 synthetic contract tests (no I/O)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from skippy.collaboration.offline_bridge02b2_contract import (
    SyntheticContractError, SyntheticInput, WorkflowSnapshot, plan_transition,
)

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
BASE, IMPLEMENTATION, CORRECTION = "1" * 40, "2" * 40, "3" * 40


def msg(kind="assignment", seq=1, **fields):
    actors = {"assignment": ("chief", "chief", "jason"),
              "implementation": ("jason", "implementer", "skippy"),
              "correction": ("jason", "implementer", "skippy"),
              "review": ("skippy", "reviewer", "jason")}
    sender, role, recipient = actors.get(kind, actors["assignment"])
    payload = {"type": kind, "evidence_id": f"fixture-{seq}"}
    if kind == "assignment":
        payload.update(objective="Synthetic validation", base_sha=BASE)
    else:
        payload["implementation_sha"] = CORRECTION if kind == "correction" else IMPLEMENTATION
    if kind == "review":
        payload["verdict"] = "approved"
    data = dict(sender=sender, role=role, recipient=recipient,
                project_id="QLA", task_id="task-test", checkpoint_id="check-test",
                assignment_id="assign-test", message_id=f"msg-{seq}",
                idempotency_key=f"idem-{seq}", sequence=seq, timestamp=NOW,
                expiry=NOW + timedelta(hours=1), payload=payload)
    data.update(fields)
    return SyntheticInput(**data)


def advance(snapshot, kind, seq, **fields):
    return plan_transition(msg(kind, seq, **fields), snapshot).next_snapshot


def test_simple_approved_workflow():
    s = advance(WorkflowSnapshot(), "assignment", 1)
    assert s.stage == "AWAIT_IMPLEMENTATION"
    s = advance(s, "implementation", 2)
    assert s.stage == "AWAIT_REVIEW"
    s = advance(s, "review", 3)
    assert s.stage == "CLOSED" and s.next_sequence == 4


def test_correction_workflow():
    s = advance(WorkflowSnapshot(), "assignment", 1)
    s = advance(s, "implementation", 2)
    review = msg("review", 3)
    review.payload["verdict"] = "changes_requested"
    s = plan_transition(review, s).next_snapshot
    assert s.stage == "AWAIT_CORRECTION"
    s = advance(s, "correction", 4)
    assert s.stage == "AWAIT_REVIEW" and s.latest_sha == CORRECTION
    reviewed = msg("review", 5)
    reviewed.payload["implementation_sha"] = CORRECTION
    assert plan_transition(reviewed, s).next_snapshot.stage == "CLOSED"


def test_exact_duplicate_is_only_a_candidate_for_gate_reverification():
    first = msg()
    s = plan_transition(first, WorkflowSnapshot()).next_snapshot
    duplicate = plan_transition(first, s)
    assert duplicate.duplicate_candidate and duplicate.next_snapshot == s
    with pytest.raises(SyntheticContractError, match="conflicting identity"):
        plan_transition(replace(first, payload={**first.payload, "objective": "changed"}), s)


@pytest.mark.parametrize("kind,seq", [("implementation", 1), ("correction", 1), ("review", 1)])
def test_assignment_is_required_first(kind, seq):
    with pytest.raises(SyntheticContractError, match="transition"):
        plan_transition(msg(kind, seq), WorkflowSnapshot())


def test_review_before_implementation_and_correction_without_request():
    s = advance(WorkflowSnapshot(), "assignment", 1)
    for kind in ("review", "correction"):
        with pytest.raises(SyntheticContractError, match="transition"):
            plan_transition(msg(kind, 2), s)


def test_closed_stream_denies_new_input():
    s = advance(WorkflowSnapshot(), "assignment", 1)
    s = advance(s, "implementation", 2)
    s = advance(s, "review", 3)
    with pytest.raises(SyntheticContractError, match="transition"):
        plan_transition(msg("implementation", 4), s)


def test_wrong_sha_and_reused_correction_sha():
    s = advance(WorkflowSnapshot(), "assignment", 1)
    s = advance(s, "implementation", 2)
    wrong = msg("review", 3)
    wrong.payload["implementation_sha"] = CORRECTION
    with pytest.raises(SyntheticContractError, match="latest"):
        plan_transition(wrong, s)
    request = msg("review", 3)
    request.payload["verdict"] = "changes_requested"
    s = plan_transition(request, s).next_snapshot
    repeated = msg("correction", 4)
    repeated.payload["implementation_sha"] = IMPLEMENTATION
    with pytest.raises(SyntheticContractError, match="must be new"):
        plan_transition(repeated, s)


@pytest.mark.parametrize("field,value", [
    ("project_id", "different"), ("task_id", "different"),
    ("checkpoint_id", "different"), ("assignment_id", "different"),
])
def test_assignment_and_stream_binding(field, value):
    s = advance(WorkflowSnapshot(), "assignment", 1)
    with pytest.raises(SyntheticContractError, match="binding mismatch"):
        plan_transition(replace(msg("implementation", 2), **{field: value}), s)


@pytest.mark.parametrize("sequence", [1, 3, 0, True])
def test_sequence_denial(sequence):
    s = advance(WorkflowSnapshot(), "assignment", 1)
    with pytest.raises(SyntheticContractError, match="sequence"):
        plan_transition(msg("implementation", sequence, message_id="fresh", idempotency_key="fresh-id"), s)


@pytest.mark.parametrize("payload_change", [
    {"type": "correction_request"}, {"sha": IMPLEMENTATION},
    {"reviewed_sha": IMPLEMENTATION}, {"base_sha": BASE},
    {"evidence_id": "  x"}, {"evidence_id": ""},
    {"implementation_sha": "A" * 40}, {"implementation_sha": "1" * 7},
    {"implementation_sha": None}, {"verdict": "approved"},
])
def test_closed_payload_rejects_unknown_fields_or_values(payload_change):
    m = msg("implementation", 2)
    m.payload.update(payload_change)
    s = advance(WorkflowSnapshot(), "assignment", 1)
    with pytest.raises(SyntheticContractError):
        plan_transition(m, s)


@pytest.mark.parametrize("verdict", ["approve", "rejected", "", "UNKNOWN"])
def test_closed_review_verdict(verdict):
    s = advance(WorkflowSnapshot(), "assignment", 1)
    s = advance(s, "implementation", 2)
    review = msg("review", 3)
    review.payload["verdict"] = verdict
    with pytest.raises(SyntheticContractError, match="verdict"):
        plan_transition(review, s)


def test_sender_role_recipient_bound_and_identity_collisions():
    s = advance(WorkflowSnapshot(), "assignment", 1)
    with pytest.raises(SyntheticContractError, match="unauthorized"):
        plan_transition(replace(msg("implementation", 2), role="chief"), s)
    with pytest.raises(SyntheticContractError, match="conflicting identity"):
        plan_transition(replace(msg("implementation", 2), message_id="msg-1"), s)
    with pytest.raises(SyntheticContractError, match="conflicting identity"):
        plan_transition(replace(msg("implementation", 2), idempotency_key="idem-1"), s)


def test_defensive_payload_snapshot_before_caller_changes():
    m = msg()
    plan = plan_transition(m, WorkflowSnapshot())
    m.payload["objective"] = "mutated later"
    assert plan.message.payload["objective"] == "Synthetic validation"


def test_reject_legacy_shape_non_plain_payload_and_non_utc():
    with pytest.raises(SyntheticContractError):
        plan_transition(object(), WorkflowSnapshot())
    with pytest.raises(SyntheticContractError):
        plan_transition(replace(msg(), payload={"type": ["assignment"]}), WorkflowSnapshot())
    with pytest.raises(SyntheticContractError, match="UTC-aware"):
        plan_transition(replace(msg(), timestamp=datetime(2026, 11, 12)), WorkflowSnapshot())
