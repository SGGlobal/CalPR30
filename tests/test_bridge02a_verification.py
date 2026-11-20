"""
BRIDGE-02A test-first verification package.

These tests demonstrate current failures against SHA d01985f.
They are expected to fail until the minimal corrections are applied.
Do not treat failures as regressions of previously passing behavior.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skippy.collaboration.authenticated_envelope import build_envelope
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


def test_live_pending_window_real_admission(tmp_path):
    """Real concurrent admission paused after PENDING commit blocks a second instance."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    paused = threading.Event()
    release = threading.Event()
    results = []
    errors = []

    adapter1 = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )

    def pause_hook():
        paused.set()
        release.wait(timeout=5)

    adapter1._after_pending_commit = pause_hook

    def first_admission():
        try:
            env = _envelope()
            adapter1.accept(env, now=_now())
            results.append("first-ok")
        except Exception as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=first_admission)
    t1.start()
    assert paused.wait(timeout=5), "First admission did not reach PENDING commit"

    # Second instance must be blocked while first is paused
    with pytest.raises(OfflineAdapterError, match="Unresolved|PENDING|Active"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )

    release.set()
    t1.join(timeout=5)
    assert "first-ok" in results
    assert not errors


def test_composite_unique_index_does_not_protect_columns(tmp_path):
    """
    CURRENT FAILURE: Composite UNIQUE is accepted as sufficient.

    A legacy schema with UNIQUE(message_id, other_col) and
    UNIQUE(idempotency_key, other_col) does not uniquely constrain
    message_id or idempotency_key alone. Current preflight unions
    columns from any unique index and passes.
    """
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    with sqlite3.connect(receipts) as c:
        c.execute(
            """
            CREATE TABLE intent_journal (
                intent_id INTEGER PRIMARY KEY,
                message_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                other_col TEXT,
                project_id TEXT, task_id TEXT, checkpoint_id TEXT,
                assignment_id TEXT, sequence INTEGER, payload_digest TEXT,
                state TEXT, recorded_at TEXT
            )
            """
        )
        c.execute("CREATE UNIQUE INDEX uq_msg ON intent_journal(message_id, other_col)")
        c.execute("CREATE UNIQUE INDEX uq_idem ON intent_journal(idempotency_key, other_col)")
    # Current preflight sees message_id and idempotency_key in unique indices
    # and passes. This should fail closed.
    with pytest.raises(OfflineAdapterError, match="UNIQUE|migration|single-column"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_extra_accepted_receipt_without_journal(tmp_path):
    """
    CURRENT FAILURE: Extra accepted receipt without matching journal is not rejected.

    An accepted receipt that has no corresponding ACCEPTED journal intent
    is not detected by the current one-direction receipt check.
    """
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    adapter.accept(env, now=_now())
    # Insert an extra accepted receipt with no matching journal intent
    with sqlite3.connect(receipts) as c:
        c.execute("DROP TRIGGER IF EXISTS audit_receipts_no_delete")
        c.execute(
            """
            INSERT INTO audit_receipts (
                message_id, idempotency_key, project_id, task_id,
                checkpoint_id, assignment_id, sender, outcome, reason, recorded_at
            ) VALUES ('orphan-msg', 'orphan-idem', 'QLA', 't', 'c', 'a', 'chief', 'accepted', NULL, '2026-11-08T12:00:00')
            """
        )
    # Current reconciliation does not reject extra receipts. This should fail closed.
    with pytest.raises(OfflineAdapterError, match="extra|orphan|receipt"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_duplicate_accepted_receipt_and_sender_mismatch(tmp_path):
    """
    CURRENT FAILURE: Duplicate receipts and sender mismatch are not rejected.

    Multiple accepted receipts for the same message ID are collapsed by
    dict comprehension. Receipt sender is not compared against ledger actor.
    """
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    adapter = OfflineAuthenticatedAdapter(
        ledger, receipts, trusted_fixture=True, halt_check=lambda: False
    )
    env = _envelope()
    adapter.accept(env, now=_now())
    # Insert a duplicate accepted receipt with a different sender
    with sqlite3.connect(receipts) as c:
        c.execute(
            """
            INSERT INTO audit_receipts (
                message_id, idempotency_key, project_id, task_id,
                checkpoint_id, assignment_id, sender, outcome, reason, recorded_at
            ) VALUES ('msg-1', 'idem-1', 'QLA', 'bridge-02', 'cp-1', 'asgn-1', 'forged-sender', 'accepted', NULL, '2026-11-08T12:00:00')
            """
        )
    # Current reconciliation collapses duplicates and does not check sender.
    # This should fail closed.
    with pytest.raises(OfflineAdapterError, match="Duplicate|duplicate|sender|mismatch"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_partial_unique_index_rejected(tmp_path):
    """Partial UNIQUE index does not independently guarantee uniqueness."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
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
        c.execute("CREATE UNIQUE INDEX uq_msg ON intent_journal(message_id) WHERE state = 'ACCEPTED'")
        c.execute("CREATE UNIQUE INDEX uq_idem ON intent_journal(idempotency_key) WHERE state = 'ACCEPTED'")
    with pytest.raises(OfflineAdapterError, match="partial|UNIQUE|migration"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )


def test_malformed_ledger_payload_rejected(tmp_path):
    """Malformed ledger payload fails closed instead of being treated as unrelated."""
    ledger = CheckpointLedger(tmp_path / "ledger.db")
    receipts = tmp_path / "receipts.db"
    # Insert a ledger event with malformed payload
    with ledger._connect() as c:
        c.execute(
            """
            INSERT INTO events (
                revision, event_type, project_id, task_id, checkpoint_id,
                actor, payload, message_id, idempotency_key, occurred_at
            ) VALUES (1, 'ASSIGNMENT_CREATED', 'QLA', 't', 'c', 'chief', '{bad json', 'malformed-msg', 'malformed-idem', '2026-11-08T12:00:00')
            """
        )
    with pytest.raises(OfflineAdapterError, match="Malformed|unreadable"):
        OfflineAuthenticatedAdapter(
            ledger, receipts, trusted_fixture=True, halt_check=lambda: False
        )
