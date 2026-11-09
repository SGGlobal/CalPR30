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


def test_fail_stop_persists_across_restart(tmp_path):
    """Fail-stop state survives restart and blocks subsequent admission."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    # Manually set fail-stop
    import sqlite3
    with sqlite3.connect(receipts) as c:
        c.execute("UPDATE fail_stop SET stopped = 1, reason = 'test' WHERE singleton = 1")
    # New instance sees the fail-stop
    adapter2 = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    with pytest.raises(OfflineAdapterError, match="Fail-stop"):
        adapter2.accept(env, now=_now())


def test_concurrent_instances_serialize(tmp_path):
    """Two instances sharing the same DB cannot both accept the same sequence."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    a1 = OfflineAuthenticatedAdapter(ledger, receipts, trusted_fixture=True, halt_check=lambda: False)
    a2 = OfflineAuthenticatedAdapter(ledger, receipts, trusted_fixture=True, halt_check=lambda: False)
    env1 = _envelope(message_id="msg-a", idempotency_key="idem-a")
    env2 = _envelope(message_id="msg-b", idempotency_key="idem-b")
    a1.accept(env1, now=_now())
    # Second instance must see advanced sequence and reject sequence=1
    with pytest.raises(OfflineAdapterError, match="Sequence"):
        a2.accept(env2, now=_now())


def test_simultaneous_concurrent_instances(tmp_path):
    """Two threads on independent instances cannot both accept the same sequence."""
    import threading
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    results = []
    errors = []

    def attempt(msg_id, idem):
        try:
            adapter = OfflineAuthenticatedAdapter(
                ledger, receipts, trusted_fixture=True, halt_check=lambda: False
            )
            env = _envelope(message_id=msg_id, idempotency_key=idem)
            adapter.accept(env, now=_now())
            results.append(msg_id)
        except Exception as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=attempt, args=("msg-t1", "idem-t1"))
    t2 = threading.Thread(target=attempt, args=("msg-t2", "idem-t2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # Exactly one should succeed
    assert len(results) == 1
    assert len(errors) == 1
    assert "Sequence" in errors[0] or "Replay" in errors[0] or "Fail-stop" in errors[0] or "PENDING" in errors[0] or "Unresolved" in errors[0]


def test_rollback_does_not_persist_accepted_on_failure(tmp_path, monkeypatch):
    """Partial failure rolls back and does not leave an accepted receipt."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    # Force sequence advance to fail after receipt insert
    original = adapter._record_receipt_in

    def failing_receipt(connection, envelope, outcome, reason, now):
        if outcome == "accepted":
            raise RuntimeError("simulated receipt failure")
        return original(connection, envelope, outcome, reason, now)

    monkeypatch.setattr(adapter, "_record_receipt_in", failing_receipt)
    env = _envelope()
    with pytest.raises(OfflineAdapterError, match="Partial write"):
        adapter.accept(env, now=_now())
    # No accepted receipt should exist
    import sqlite3
    with sqlite3.connect(receipts) as c:
        rows = c.execute(
            "SELECT outcome FROM audit_receipts WHERE outcome = 'accepted'"
        ).fetchall()
    assert len(rows) == 0


def test_reconciliation_detects_missing_receipt(tmp_path):
    """Startup reconciliation fails closed when intent journal is missing but adapter ledger events exist."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    adapter.accept(env, now=_now())

    # Simulate crash: remove the intent journal database while leaving the ledger
    receipts.unlink()

    # New instance must fail closed on reconciliation
    with pytest.raises(OfflineAdapterError, match="unreadable|mismatch|reconciliation|intent"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_reconciliation_blocks_subsequent_admission(tmp_path):
    """After mismatch, subsequent admission is blocked even after restart."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    adapter.accept(env, now=_now())

    # Remove receipts DB to simulate divergence
    receipts.unlink()

    # Reconciliation fails
    with pytest.raises(OfflineAdapterError):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_pending_intent_blocks_restart(tmp_path):
    """A PENDING intent without resolution fails closed on restart."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    # Manually insert a PENDING intent
    import sqlite3
    with sqlite3.connect(receipts) as c:
        c.execute(
            """
            INSERT INTO intent_journal (
                message_id, idempotency_key, project_id, task_id,
                checkpoint_id, assignment_id, sequence, payload_digest,
                state, recorded_at
            ) VALUES ('pending-msg', 'pending-idem', 'QLA', 't', 'c', 'a', 1, 'd', 'PENDING', '2026-11-08T12:00:00')
            """
        )
    # New instance must fail closed
    with pytest.raises(OfflineAdapterError, match="Unresolved|PENDING|Active"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_mixed_origin_events_ignored(tmp_path):
    """Independent BRIDGE-01 events without adapter origin are ignored by reconciliation."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    # Direct ledger event without adapter origin
    ledger.record_event(
        event_type=__import__("skippy.collaboration.checkpoint_ledger", fromlist=["EventType"]).EventType.ASSIGNMENT_CREATED,
        project_id="QLA",
        task_id="other",
        checkpoint_id="cp-other",
        actor="chief",
        payload={"assignment_id": "other", "synthetic": True},
        message_id="independent-msg",
        idempotency_key="independent-idem",
        expected_revision=0,
        now=_now(),
    )
    # Adapter should start cleanly — independent event is out of scope
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    result = adapter.accept(env, now=_now())
    assert result == "accepted"


def test_uncertain_blocks_restart(tmp_path):
    """UNCERTAIN intent blocks subsequent admission and restart."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    import sqlite3
    with sqlite3.connect(receipts) as c:
        c.execute(
            """
            INSERT INTO intent_journal (
                message_id, idempotency_key, project_id, task_id,
                checkpoint_id, assignment_id, sequence, payload_digest,
                state, recorded_at
            ) VALUES ('uncertain-msg', 'uncertain-idem', 'QLA', 't', 'c', 'a', 1, 'd', 'UNCERTAIN', '2026-11-08T12:00:00')
            """
        )
    with pytest.raises(OfflineAdapterError, match="Unresolved|UNCERTAIN|Active"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_active_pending_blocks_second_instance(tmp_path):
    """An active PENDING intent blocks a second adapter instance."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    import sqlite3
    with sqlite3.connect(receipts) as c:
        c.execute(
            """
            INSERT INTO intent_journal (
                message_id, idempotency_key, project_id, task_id,
                checkpoint_id, assignment_id, sequence, payload_digest,
                state, recorded_at
            ) VALUES ('active-msg', 'active-idem', 'QLA', 't', 'c', 'a', 1, 'd', 'PENDING', '2026-11-08T12:00:00')
            """
        )
    # Second instance must fail on reconciliation
    with pytest.raises(OfflineAdapterError, match="Unresolved|PENDING|Active"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_identity_mismatch_digest_rejected(tmp_path):
    """Matching message ID with conflicting digest fails reconciliation."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    adapter.accept(env, now=_now())
    # Tamper with the journal digest
    import sqlite3
    with sqlite3.connect(receipts) as c:
        c.execute("UPDATE intent_journal SET payload_digest = 'tampered' WHERE message_id = 'msg-1'")
    with pytest.raises(OfflineAdapterError, match="Identity mismatch|mismatch"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_missing_accepted_receipt_rejected(tmp_path):
    """ACCEPTED journal without accepted receipt fails reconciliation."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    adapter.accept(env, now=_now())
    import sqlite3
    # Bypass append-only trigger by dropping and recreating without the receipt
    with sqlite3.connect(receipts) as c:
        c.execute("DROP TRIGGER IF EXISTS audit_receipts_no_delete")
        c.execute("DELETE FROM audit_receipts WHERE outcome = 'accepted'")
    with pytest.raises(OfflineAdapterError, match="Missing accepted receipt"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_legacy_schema_without_unique_rejected(tmp_path):
    """Existing journal without UNIQUE constraints fails preflight."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    import sqlite3
    with sqlite3.connect(receipts) as c:
        c.execute(
            """
            CREATE TABLE intent_journal (
                intent_id INTEGER PRIMARY KEY,
                message_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                project_id TEXT, task_id TEXT, checkpoint_id TEXT,
                assignment_id TEXT, sequence INTEGER, payload_digest TEXT,
                state TEXT, recorded_at TEXT
            )
            """
        )
    with pytest.raises(OfflineAdapterError, match="UNIQUE|migration"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )
