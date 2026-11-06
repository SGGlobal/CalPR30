"""
SKIPPY AI Collaboration Bridge — Protocol Tests.

LOCAL SYNTHETIC ONLY.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skippy.collaboration.protocol import (
    ApprovalRecord,
    CollaborationProtocol,
    Message,
    MessageType,
    ProtocolError,
    TaskState,
)


def make_message(
    task_id: str = "T1",
    message_type: MessageType = MessageType.TASK_ASSIGNMENT,
    idempotency_key: str = "key-1",
    payload: dict | None = None,
    timestamp: datetime | None = None,
    sender: str = "skyppy",
    recipient: str = "jason",
    message_id: str | None = None,
) -> Message:
    return Message(
        message_id=message_id or f"msg-{idempotency_key}-{message_type.value}",
        idempotency_key=idempotency_key,
        correlation_id=f"corr-{task_id}",
        project_id="QLA",
        task_id=task_id,
        sender=sender,
        recipient=recipient,
        message_type=message_type,
        timestamp=timestamp or datetime.now(timezone.utc),
        payload=payload or {"step": "plan"},
    )


def test_valid_message_accepted_and_persisted(tmp_path):
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)

    msg = make_message()
    assert protocol.accept(msg) is True
    assert protocol.get_state("T1") is TaskState.ASSIGNED

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT message_id FROM messages WHERE message_id = ?",
            (msg.message_id,),
        ).fetchone()

    assert row is not None


def test_successful_round_trip(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    messages = protocol.simulate_round_trip(
        task_id="T1",
        assignment_payload={"objective": "design"},
        result_payload={"plan": "local protocol"},
    )

    assert len(messages) == 5
    assert protocol.get_state("T1") is TaskState.UNDER_REVIEW
    assert all(protocol.is_acknowledged(m.message_id) for m in messages)


def test_persistence_after_reopen(tmp_path):
    database = tmp_path / "bridge.db"
    first = CollaborationProtocol(database)

    msg = make_message()
    first.accept(msg)
    first.acknowledge(msg.message_id)

    second = CollaborationProtocol(database)

    assert second.get_state("T1") is TaskState.ASSIGNED
    assert second.is_acknowledged(msg.message_id) is True
    assert second.unacknowledged() == []


def test_identical_messages_recognized_as_duplicates(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    msg = make_message()
    assert protocol.accept(msg) is True
    assert protocol.accept(msg) is False


def test_conflicting_message_id_different_timestamp(tmp_path):
    """
    Two messages share a message_id but differ in timestamp.

    Both timestamps are inside the default TTL so the failure is an
    envelope conflict, not expiration.
    """
    now = datetime.now(timezone.utc)
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    first = make_message(
        timestamp=now - timedelta(seconds=10),
    )
    assert protocol.accept(first, now=now) is True

    conflict = make_message(
        message_id=first.message_id,
        idempotency_key=first.idempotency_key,
        timestamp=now - timedelta(seconds=5),
    )

    with pytest.raises(ProtocolError, match="Conflicting content for existing message ID"):
        protocol.accept(conflict, now=now)


def test_conflicting_message_id_rejected(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    first = make_message(payload={"a": 1})
    protocol.accept(first)

    conflict = make_message(
        message_id=first.message_id,
        idempotency_key="different-key",
        payload={"a": 2},
    )

    with pytest.raises(ProtocolError, match="Conflicting content for existing message ID"):
        protocol.accept(conflict)


def test_conflicting_idempotency_key_rejected(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    first = make_message(task_id="T1", idempotency_key="shared")
    protocol.accept(first)

    conflict = make_message(
        task_id="T2",
        idempotency_key="shared",
        message_id="other-id",
    )

    with pytest.raises(ProtocolError, match="Conflicting content for existing idempotency key"):
        protocol.accept(conflict)


def test_legitimate_repeated_operations_distinguishable(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    messages = protocol.simulate_round_trip(
        "T2",
        {"objective": "x"},
        {"plan": "y"},
    )

    keys = {m.idempotency_key for m in messages}
    ids = {m.message_id for m in messages}

    assert len(keys) == 5
    assert len(ids) == 5


def test_mutating_original_payload_does_not_change_identity(tmp_path):
    """
    Mutating the caller's nested dict after Message construction must not
    change the message's identity or the stored payload.
    """
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)

    original = {"step": "plan", "nested": {"value": 1}}
    msg = make_message(payload=original)

    identity_before = msg.envelope_identity()
    canonical_before = msg.canonical_payload

    # Mutate the original nested structure.
    original["nested"]["value"] = 999
    original["extra"] = "should-not-appear"

    assert msg.envelope_identity() == identity_before
    assert msg.canonical_payload == canonical_before

    protocol.accept(msg)

    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT payload_json FROM messages WHERE message_id = ?",
            (msg.message_id,),
        ).fetchone()[0]

    assert stored == canonical_before
    assert "999" not in stored
    assert "should-not-appear" not in stored


def test_non_string_keys_rejected():
    with pytest.raises(ProtocolError, match="keys must be strings"):
        make_message(payload={1: "bad"})


def test_non_finite_payload_rejected(tmp_path):
    database = tmp_path / "bridge.db"
    CollaborationProtocol(database)

    with pytest.raises(ProtocolError, match="non-finite"):
        make_message(payload={"price": float("nan")})

    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    assert count == 0


def test_invalid_transition_rejected(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    protocol.accept(make_message())

    complete = make_message(
        message_type=MessageType.COMPLETION,
        idempotency_key="complete-1",
    )

    with pytest.raises(ProtocolError, match="Invalid transition"):
        protocol.accept(complete)


def test_sqlite_rollback_on_persistence_failure(tmp_path):
    """
    Use an SQLite trigger to abort INSERT on task_states.

    The message INSERT begins, the task-state INSERT fails, and the
    transaction rolls back. Historical records remain unchanged.
    """
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)

    historical = make_message(task_id="HIST", idempotency_key="hist")
    protocol.accept(historical)

    # Install a trigger that aborts any new task_states insert.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER abort_task_states
            BEFORE INSERT ON task_states
            BEGIN
                SELECT RAISE(ABORT, 'injected task_states failure');
            END;
            """
        )

    failing = make_message(task_id="NEW", idempotency_key="new")

    with pytest.raises(ProtocolError, match="Persistence failed"):
        protocol.accept(failing)

    with sqlite3.connect(database) as connection:
        messages = connection.execute(
            "SELECT message_id FROM messages"
        ).fetchall()
        states = connection.execute(
            "SELECT task_id FROM task_states"
        ).fetchall()

    assert [row[0] for row in messages] == [historical.message_id]
    assert [row[0] for row in states] == ["HIST"]


def test_halt_by_one_instance_blocks_another(tmp_path):
    database = tmp_path / "bridge.db"

    first = CollaborationProtocol(database)
    second = CollaborationProtocol(database)

    first.halt()

    with pytest.raises(ProtocolError, match="halted"):
        second.accept(make_message())


def test_halt_blocks_outbound_across_instances(tmp_path):
    database = tmp_path / "bridge.db"

    first = CollaborationProtocol(database)
    second = CollaborationProtocol(database)

    first.halt()

    with pytest.raises(ProtocolError, match="halted"):
        second.track_outbound("T1")


def test_unacknowledged_recovery(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    msg = make_message()

    protocol.accept(msg)
    assert protocol.is_acknowledged(msg.message_id) is False

    pending = protocol.unacknowledged()
    assert msg.message_id in pending

    protocol.acknowledge(msg.message_id)
    assert protocol.unacknowledged() == []


def test_synthetic_approval_does_not_authorize_real_actions(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    approval = ApprovalRecord(
        approval_id="A1",
        chief_identity="chief",
        action="MERGE_PR",
        task_id="T1",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    protocol.record_approval(approval)
    consumed = protocol.consume_approval("A1")

    assert consumed.consumed_at is not None
    assert consumed.action == "MERGE_PR"


def test_approval_consumed_once(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    approval = ApprovalRecord(
        approval_id="A2",
        chief_identity="chief",
        action="DEPLOY",
        task_id="T1",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    protocol.record_approval(approval)
    protocol.consume_approval("A2")

    with pytest.raises(ProtocolError, match="already consumed"):
        protocol.consume_approval("A2")


def test_unsupported_protocol_version():
    with pytest.raises(ProtocolError, match="Unsupported protocol version"):
        Message(
            message_id="x",
            idempotency_key="k",
            correlation_id="c",
            project_id="QLA",
            task_id="T",
            sender="skyppy",
            recipient="jason",
            message_type=MessageType.TASK_ASSIGNMENT,
            timestamp=datetime.now(timezone.utc),
            protocol_version="0.9",
        )


def test_invalid_timestamp():
    with pytest.raises(ProtocolError, match="timezone-aware"):
        Message(
            message_id="x",
            idempotency_key="k",
            correlation_id="c",
            project_id="QLA",
            task_id="T",
            sender="skyppy",
            recipient="jason",
            message_type=MessageType.TASK_ASSIGNMENT,
            timestamp=datetime.now(),
        )


def test_ai_cannot_claim_approved():
    with pytest.raises(ProtocolError, match="cannot establish an approved"):
        Message(
            message_id="x",
            idempotency_key="k",
            correlation_id="c",
            project_id="QLA",
            task_id="T",
            sender="skyppy",
            recipient="jason",
            message_type=MessageType.TASK_ASSIGNMENT,
            timestamp=datetime.now(timezone.utc),
            authorization_status="APPROVED",
        )


def test_expiration_rejected(tmp_path):
    protocol = CollaborationProtocol(
        tmp_path / "bridge.db",
        message_ttl_seconds=10,
    )
    old = datetime.now(timezone.utc) - timedelta(seconds=60)

    msg = make_message(timestamp=old)

    with pytest.raises(ProtocolError, match="expired"):
        protocol.accept(msg, now=datetime.now(timezone.utc))


def test_message_bound_enforced(tmp_path):
    protocol = CollaborationProtocol(
        tmp_path / "bridge.db",
        max_messages_per_task=2,
    )

    protocol.accept(make_message(idempotency_key="k1"))
    protocol.accept(
        make_message(
            message_type=MessageType.ACK,
            idempotency_key="k2",
            sender="jason",
            recipient="skyppy",
        )
    )

    with pytest.raises(ProtocolError, match="Maximum"):
        protocol.accept(
            make_message(
                message_type=MessageType.START,
                idempotency_key="k3",
                sender="jason",
                recipient="bridge",
            )
        )
