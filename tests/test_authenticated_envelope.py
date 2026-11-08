"""
SKIPPY AI Collaboration Bridge — BRIDGE-02A Offline Contract Tests.

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
        chief_authorization=True,
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


def test_replay_rejected(tmp_path):
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    adapter = OfflineAuthenticatedAdapter(ledger, tmp_path / "receipts.db")
    env = _envelope()
    adapter.accept(env, now=_now())
    with pytest.raises(OfflineAdapterError, match="Replay"):
        adapter.accept(env, now=_now())


def test_digest_mismatch_rejected():
    env = _envelope()
    # Tamper with payload after construction
    object.__setattr__(env, "payload", {"type": "assignment", "objective": "tampered"})
    with pytest.raises(EnvelopeError, match="digest"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_unauthorized_sender_rejected():
    env = _envelope(sender="unknown", role="implementer")
    with pytest.raises(EnvelopeError, match="Unauthorized sender"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_expired_envelope_rejected():
    env = _envelope(expiry=_now() - timedelta(minutes=1))
    with pytest.raises(EnvelopeError, match="expired"):
        validate_envelope(env, now=_now(), seen_message_ids=set(), seen_idempotency_keys=set())


def test_assignment_requires_chief_authorization():
    env = _envelope(chief_authorization=False)
    with pytest.raises(EnvelopeError, match="Chief authorization"):
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
