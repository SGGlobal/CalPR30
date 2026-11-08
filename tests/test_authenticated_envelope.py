"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A.1 Offline Contract Tests.

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
        trusted_fixture=True,
    )
    defaults.update(overrides)
    return build_envelope(**defaults)


def test_legitimate_assignment_accepted(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db")
    env = _envelope()
    result = adapter.accept(env, now=_now())
    assert result == "accepted"
    assert adapter.execution_enabled is False
    assert adapter.TRANSPORT_ENABLED is False


def test_replay_rejected_and_durable_after_restart(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(ledger, receipts)
    env = _envelope()
    adapter.accept(env, now=_now())
    # Restart
    adapter2 = OfflineAuthenticatedAdapter(ledger, receipts)
    with pytest.raises(OfflineAdapterError, match="Replay"):
        adapter2.accept(env, now=_now())


def test_role_spoofing_assignment_rejected():
    """Implementer role with assignment type is rejected (no bypass)."""
    env = _envelope(sender="jason", role="implementer", recipient="skippy")
    with pytest.raises(EnvelopeError, match="Unauthorized"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_unknown_payload_type_rejected():
    env = _envelope(payload={"type": "unknown_kind"})
    with pytest.raises(EnvelopeError, match="Unauthorized"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_digest_mismatch_rejected():
    env = _envelope()
    # Reconstruct with different payload_json would fail digest; simulate by checking
    from dataclasses import replace
    tampered = replace(env, payload_digest="0" * 64)
    with pytest.raises(EnvelopeError, match="digest"):
        validate_envelope(tampered, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_unauthorized_sender_rejected():
    env = _envelope(sender="unknown", role="implementer", payload={"type": "implementation"})
    with pytest.raises(EnvelopeError, match="Unauthorized sender"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_expired_envelope_rejected():
    env = _envelope(expiry=_now() - timedelta(minutes=1), timestamp=_now() - timedelta(hours=2))
    with pytest.raises(EnvelopeError, match="expired|too old"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_assignment_requires_trusted_fixture():
    env = _envelope(trusted_fixture=False)
    with pytest.raises(EnvelopeError, match="trusted fixture"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_sequence_mismatch_rejected():
    env = _envelope(sequence=5)
    with pytest.raises(EnvelopeError, match="Sequence"):
        validate_envelope(
            env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set(),
            expected_sequence=1,
        )


def test_malformed_identity_rejected():
    env = _envelope(project_id="")
    with pytest.raises(EnvelopeError, match="identity"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_halted_envelope_rejected(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db")
    env = _envelope()
    with pytest.raises(OfflineAdapterError, match="HALTED"):
        adapter.accept(env, now=_now(), halted=True)


def test_nonzero_ledger_revision_accepted(tmp_path):
    """Sequence and ledger revision are independent."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db")
    # First event advances revision
    env1 = _envelope(message_id="msg-1", idempotency_key="idem-1", sequence=1)
    adapter.accept(env1, now=_now())
    # Second event with sequence 2 uses actual ledger revision
    env2 = _envelope(
        message_id="msg-2",
        idempotency_key="idem-2",
        sequence=2,
        sender="jason",
        recipient="skippy",
        role="implementer",
        payload={"type": "implementation"},
    )
    result = adapter.accept(env2, now=_now())
    assert result == "accepted"
