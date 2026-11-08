"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A.2 Offline Contract Tests.

Synthetic. Offline. Non-executing.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skippy.collaboration.authenticated_envelope import (
    EnvelopeError,
    build_envelope,
    validate_envelope,
)
from skippy.collaboration.checkpoint_ledger import CheckpointLedger
from skippy.collaboration.offline_authenticated_adapter import (
    OfflineAdapterError,
    OfflineAuthenticatedAdapter,
)


def _now():
    return datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _envelope(**overrides):
    defaults = dict(
        project_id="QLA",
        task_id="bridge-02",
        checkpoint_id="cp-1",
        assignment_id="asgn-1",
        message_id="msg-1",
        idempotency_key="idem-1",
        sender="chief",
        recipient="jason",
        role="chief",
        payload={"type": "assignment", "objective": "test"},
        timestamp=_now(),
        expiry=_now() + timedelta(hours=1),
        sequence=1,
    )
    defaults.update(overrides)
    return build_envelope(**defaults)


def test_legitimate_assignment_accepted(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db", trusted_fixture=True, halt_check=lambda: False)
    env = _envelope()
    result = adapter.accept(env, now=_now())
    assert result == "accepted"
    assert adapter.execution_enabled is False
    assert adapter.TRANSPORT_ENABLED is False


def test_assignment_without_trusted_fixture_rejected(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db", trusted_fixture=False, halt_check=lambda: False)
    env = _envelope()
    with pytest.raises(OfflineAdapterError, match="trusted fixture"):
        adapter.accept(env, now=_now())


def test_replay_rejected_and_durable_after_restart(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(ledger, receipts, trusted_fixture=True, halt_check=lambda: False)
    env = _envelope()
    adapter.accept(env, now=_now())
    adapter2 = OfflineAuthenticatedAdapter(ledger, receipts, trusted_fixture=True, halt_check=lambda: False)
    with pytest.raises(OfflineAdapterError, match="Replay"):
        adapter2.accept(env, now=_now())


def test_sequence_gap_rejected(tmp_path):
    """Sequence 999 is rejected when expected is 1."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db", trusted_fixture=True, halt_check=lambda: False)
    env = _envelope(sequence=999)
    with pytest.raises(OfflineAdapterError, match="Sequence"):
        adapter.accept(env, now=_now())


def test_role_spoofing_assignment_rejected():
    env = _envelope(sender="jason", role="implementer", recipient="skippy", payload={"type": "assignment", "objective": "x"})
    with pytest.raises(EnvelopeError, match="Unauthorized"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(), expected_sequence=1)


def test_unknown_payload_type_rejected():
    env = _envelope(payload={"type": "unknown_kind"})
    with pytest.raises(EnvelopeError, match="Unauthorized"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(), expected_sequence=1)


def test_digest_mismatch_rejected():
    env = _envelope()
    from dataclasses import replace
    tampered = replace(env, payload_digest="0" * 64)
    with pytest.raises(EnvelopeError, match="digest"):
        validate_envelope(tampered, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(), expected_sequence=1)


def test_unauthorized_sender_rejected():
    env = _envelope(sender="unknown", role="implementer", recipient="skippy", payload={"type": "implementation", "sha": "abc1234"})
    with pytest.raises(EnvelopeError, match="Unauthorized sender"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(), expected_sequence=1)


def test_expired_envelope_rejected():
    env = _envelope(expiry=_now() - timedelta(minutes=1), timestamp=_now() - timedelta(hours=2))
    with pytest.raises(EnvelopeError, match="expired|too old"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(), expected_sequence=1)


def test_malformed_identity_rejected():
    env = _envelope(project_id="")
    with pytest.raises(EnvelopeError, match="identity"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(), expected_sequence=1)


def test_halted_envelope_rejected(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(
        ledger, tmp_path / "receipts.db", trusted_fixture=True, halt_check=lambda: True
    )
    env = _envelope()
    with pytest.raises(OfflineAdapterError, match="HALTED"):
        adapter.accept(env, now=_now())


def test_implementation_preserves_sha(tmp_path):
    """Implementation SHA is preserved in the ledger payload."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db", trusted_fixture=True, halt_check=lambda: False)
    # Assignment
    env1 = _envelope()
    adapter.accept(env1, now=_now())
    # Implementation with SHA
    env2 = _envelope(
        message_id="msg-2",
        idempotency_key="idem-2",
        sequence=2,
        sender="jason",
        recipient="skippy",
        role="implementer",
        payload={"type": "implementation", "sha": "abc1234"},
    )
    adapter.accept(env2, now=_now())
    status = ledger.status("QLA", "bridge-02")
    assert status.current_sha == "abc1234"


def test_review_preserves_sha_and_source_verified(tmp_path):
    """Review SHA and source_verified are preserved."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db", trusted_fixture=True, halt_check=lambda: False)
    env1 = _envelope()
    adapter.accept(env1, now=_now())
    env2 = _envelope(
        message_id="msg-2", idempotency_key="idem-2", sequence=2,
        sender="jason", recipient="skippy", role="implementer",
        payload={"type": "implementation", "sha": "abc1234"},
    )
    adapter.accept(env2, now=_now())
    env3 = _envelope(
        message_id="msg-3", idempotency_key="idem-3", sequence=3,
        sender="skippy", recipient="jason", role="reviewer",
        payload={"type": "review", "sha": "abc1234", "source_verified": True},
    )
    adapter.accept(env3, now=_now())
    status = ledger.status("QLA", "bridge-02")
    assert status.last_reviewed_sha == "abc1234"
    assert status.state.value == "SOURCE_VERIFIED"


def test_adapter_requires_halt_check(tmp_path):
    """Constructing adapter without halt_check is rejected."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    with pytest.raises(OfflineAdapterError, match="HALT check"):
        OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db", trusted_fixture=True)


def test_assignment_identity_mismatch_rejected(tmp_path):
    """Different assignment ID on the same project/task is rejected."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(
        ledger, tmp_path / "receipts.db", trusted_fixture=True, halt_check=lambda: False
    )
    env1 = _envelope()
    adapter.accept(env1, now=_now())
    env2 = _envelope(
        message_id="msg-2", idempotency_key="idem-2", sequence=2,
        assignment_id="different-asgn",
        sender="jason", recipient="skippy", role="implementer",
        payload={"type": "implementation", "sha": "abc1234"},
    )
    with pytest.raises(OfflineAdapterError, match="Assignment identity"):
        adapter.accept(env2, now=_now())


def test_invalid_sha_format_rejected():
    env = _envelope(
        sender="jason", recipient="skippy", role="implementer",
        payload={"type": "implementation", "sha": "not-a-sha"},
    )
    with pytest.raises(EnvelopeError, match="Invalid"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(), expected_sequence=1)
