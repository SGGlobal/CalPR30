"""
SKIPPY AI Collaboration Bridge — Protocol Tests.

LOCAL SYNTHETIC ONLY.
"""

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


def test_unacknowledged_recovery(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    msg = make_message()

    protocol.accept(msg)
    assert protocol.is_acknowledged(msg.message_id) is False

    pending = protocol.unacknowledged()
    assert msg.message_id in pending

    protocol.acknowledge(msg.message_id)
    assert protocol.unacknowledged() == []


def test_duplicate_message_id_with_conflicting_content(tmp_path):
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


def test_same_idempotency_key_different_task(tmp_path):
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


def test_valid_duplicate_delivery(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    msg = make_message()
    assert protocol.accept(msg) is True
    assert protocol.accept(msg) is False


def test_invalid_transition_rejected(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    protocol.accept(make_message())

    complete = make_message(
        message_type=MessageType.COMPLETION,
        idempotency_key="complete-1",
    )

    with pytest.raises(ProtocolError, match="Invalid transition"):
        protocol.accept(complete)


def test_complete_round_trip_without_direct_mutation(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")

    messages = protocol.simulate_round_trip(
        "T3",
        {"objective": "x"},
        {"plan": "y"},
    )

    # All transitions went through accept().
    assert protocol.get_state("T3") is TaskState.UNDER_REVIEW
    assert len(messages) == 5


def test_persistent_halt(tmp_path):
    database = tmp_path / "bridge.db"
    first = CollaborationProtocol(database)
    first.halt()

    second = CollaborationProtocol(database)
    assert second.is_halted is True

    with pytest.raises(ProtocolError, match="halted"):
        second.accept(make_message())


def test_outbound_rejected_after_halt(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    protocol.halt()

    with pytest.raises(ProtocolError, match="halted"):
        protocol.track_outbound("T1")


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
            timestamp=datetime.now(),  # naive
        )


def test_ai_cannot_claim_approved(tmp_path):
    with pytest.raises(ProtocolError, match="cannot establish an approved"):
        make_message(). __class__(
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


def test_synthetic_approval_does_not_authorize_real_actions(tmp_path):
    """
    Consuming a synthetic approval updates only the local record.
    It does not produce any external side effect or real authorization.
    """
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
    # No external action was taken; the record is local only.
    assert consumed.action == "MERGE_PR"


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
