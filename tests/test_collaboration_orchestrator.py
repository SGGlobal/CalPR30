"""
SKIPPY AI Collaboration Bridge — Orchestrator Tests.

LOCAL SYNTHETIC ONLY.
"""

from datetime import datetime, timezone

import pytest

from skippy.collaboration.orchestrator import CollaborationOrchestrator
from skippy.collaboration.protocol import (
    CollaborationProtocol,
    Message,
    MessageType,
    ProtocolError,
    TaskState,
)
from skippy.collaboration.router import LocalRouter, RouterError


def test_successful_round_trip(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    report = orchestrator.run("T1", "design local protocol")

    assert report["status"] == "COMPLETE"
    assert report["outcome"] == "approved"
    assert report["corrections"] == 0
    assert protocol.get_state("T1") is TaskState.COMPLETE


def test_correction_loop(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    report = orchestrator.run(
        "T2",
        "design with correction",
        force_correction=True,
    )

    assert report["status"] == "COMPLETE"
    assert report["outcome"] == "approved"
    assert report["corrections"] == 1


def test_handler_invoked_on_delivery(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    router = LocalRouter(protocol)

    called = []

    def handler(message):
        called.append(message.message_id)

    router.register("jason", handler)

    msg = Message(
        message_id="h1", idempotency_key="h-k", correlation_id="c",
        project_id="QLA", task_id="TH", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT,
        timestamp=datetime.now(timezone.utc),
        payload={"objective": "handle me"},
    )

    protocol.accept(msg)
    assert router.dispatch(msg) == "handled"
    assert called == ["h1"]


def test_handler_failure_leaves_unacknowledged(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    def failing(message):
        raise RuntimeError("handler failed")

    orchestrator.router.register("jason", failing)

    now = datetime.now(timezone.utc)
    msg = Message(
        message_id="f1", idempotency_key="f-k", correlation_id="c",
        project_id="QLA", task_id="TF", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT, timestamp=now,
        payload={"objective": "fail"},
    )

    with pytest.raises(RuntimeError, match="handler failed"):
        orchestrator._send(msg, now)

    assert protocol.is_acknowledged("f1") is False
    assert "f1" in protocol.unacknowledged()


def test_duplicate_handling_does_not_repeat_effects(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    router = LocalRouter(protocol)

    calls = []

    def handler(message):
        calls.append(message.message_id)

    router.register("jason", handler)

    msg = Message(
        message_id="d1", idempotency_key="d-k", correlation_id="c",
        project_id="QLA", task_id="TD", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT,
        timestamp=datetime.now(timezone.utc),
        payload={"objective": "once"},
    )

    protocol.accept(msg)
    assert router.dispatch(msg) == "handled"
    assert router.dispatch(msg) == "duplicate"
    assert calls == ["d1"]


def test_recovery_after_assignment(tmp_path):
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)

    now = datetime.now(timezone.utc)
    msg = Message(
        message_id="assign-1",
        idempotency_key="assign-T3",
        correlation_id="corr-T3",
        project_id="QLA",
        task_id="T3",
        sender="skyppy",
        recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT,
        timestamp=now,
        payload={"objective": "partial"},
    )
    protocol.accept(msg, now=now)
    protocol.acknowledge(msg.message_id)

    recovered = CollaborationOrchestrator(CollaborationProtocol(database))
    status = recovered.recover("T3")

    assert status["recovery"] == "resumable"
    assert status["status"] == "ASSIGNED"
    assert status["correlation_id"] == "corr-T3"


def test_recovery_after_ack(tmp_path):
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)
    now = datetime.now(timezone.utc)

    assignment = Message(
        message_id="a1", idempotency_key="k-a", correlation_id="c",
        project_id="QLA", task_id="T4", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT, timestamp=now,
        payload={"objective": "x"},
    )
    protocol.accept(assignment, now=now)
    protocol.acknowledge(assignment.message_id)

    ack = Message(
        message_id="a2", idempotency_key="k-ack", correlation_id="c",
        project_id="QLA", task_id="T4", sender="jason", recipient="skyppy",
        message_type=MessageType.ACK, timestamp=now,
        payload={"received": True}, parent_message_id="a1",
    )
    protocol.accept(ack, now=now)
    protocol.acknowledge(ack.message_id)

    status = CollaborationOrchestrator(
        CollaborationProtocol(database)
    ).recover("T4")

    assert status["recovery"] == "resumable"
    assert status["status"] == "ACKNOWLEDGED"


def test_earlier_unacknowledged_blocks_recovery(tmp_path):
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)
    now = datetime.now(timezone.utc)

    first = Message(
        message_id="early", idempotency_key="k-early", correlation_id="c",
        project_id="QLA", task_id="TE", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT, timestamp=now,
        payload={"objective": "early"},
    )
    protocol.accept(first, now=now)
    # Intentionally not acknowledged.

    status = CollaborationOrchestrator(
        CollaborationProtocol(database)
    ).recover("TE")

    assert status["recovery"] == "uncertain"
    assert "early" in status["outstanding"]


def test_recovery_after_result(tmp_path):
    database = tmp_path / "bridge.db"
    first = CollaborationProtocol(database)
    CollaborationOrchestrator(first).run("T5", "result stage")

    status = CollaborationOrchestrator(
        CollaborationProtocol(database)
    ).recover("T5")

    assert status["recovery"] == "complete_not_replayed"


def test_completed_task_not_replayed(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)
    orchestrator.run("T6", "done")

    with pytest.raises(ProtocolError, match="not replayed"):
        orchestrator.run("T6", "again")


def test_report_accurate_after_reopen(tmp_path):
    database = tmp_path / "bridge.db"
    first = CollaborationOrchestrator(CollaborationProtocol(database))
    first.run("T8", "report me", force_correction=True)

    second = CollaborationOrchestrator(CollaborationProtocol(database))
    report = second.report("T8")

    assert report["status"] == "COMPLETE"
    assert report["outcome"] == "approved"
    assert report["corrections"] == 1
    assert report["message_count"] > 0


def test_task_specific_filtering(tmp_path):
    database = tmp_path / "bridge.db"
    orchestrator = CollaborationOrchestrator(CollaborationProtocol(database))
    orchestrator.run("T9", "one")
    orchestrator.run("T10", "two", force_correction=True)

    first = orchestrator.report("T9")
    second = orchestrator.report("T10")

    assert first["task_id"] == "T9"
    assert second["task_id"] == "T10"
    assert first["message_count"] > 0
    assert second["message_count"] > first["message_count"]
    assert first["corrections"] == 0
    assert second["corrections"] == 1
    assert first["unacknowledged"] == []
    assert second["unacknowledged"] == []


def test_halt_during_active_workflow(tmp_path):
    """
    Issue HALT after the assignment is accepted and before the next stage.
    Further agent handling and message acceptance must stop.
    """
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)
    orchestrator = CollaborationOrchestrator(protocol)
    now = datetime.now(timezone.utc)

    assignment = Message(
        message_id="halt-1", idempotency_key="halt-k", correlation_id="c",
        project_id="QLA", task_id="THALT", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT, timestamp=now,
        payload={"objective": "interrupted"},
    )
    protocol.accept(assignment, now=now)
    protocol.acknowledge(assignment.message_id)

    protocol.halt()

    with pytest.raises(ProtocolError, match="halted"):
        orchestrator._send(
            Message(
                message_id="halt-2", idempotency_key="halt-k2",
                correlation_id="c", project_id="QLA", task_id="THALT",
                sender="jason", recipient="skyppy",
                message_type=MessageType.ACK, timestamp=now,
                payload={"received": True}, parent_message_id="halt-1",
            ),
            now,
        )

    # Previously accepted record remains inspectable.
    assert orchestrator.report("THALT")["status"] == "ASSIGNED"
    assert orchestrator.report("THALT")["message_count"] == 1

    reopened = CollaborationOrchestrator(CollaborationProtocol(database))
    assert reopened.protocol.is_halted is True
    assert reopened.report("THALT")["status"] == "ASSIGNED"


def test_halt_persists_across_reopen(tmp_path):
    database = tmp_path / "bridge.db"
    CollaborationProtocol(database).halt()

    second = CollaborationOrchestrator(CollaborationProtocol(database))
    assert second.protocol.is_halted is True

    with pytest.raises(ProtocolError, match="halted"):
        second.run("T13", "still halted")


def test_invalid_recipient_rejected():
    """
    Message construction rejects unknown recipients.
    Router validation is tested with a valid Message that is then
    checked by validate_recipient after a synthetic override is not
    possible; instead we confirm the constructor itself rejects.
    """
    with pytest.raises(ProtocolError, match="Unknown recipient"):
        Message(
            message_id="bad", idempotency_key="bad-k", correlation_id="c",
            project_id="QLA", task_id="T14", sender="skyppy",
            recipient="unknown-agent",
            message_type=MessageType.TASK_ASSIGNMENT,
            timestamp=datetime.now(timezone.utc),
            payload={"objective": "x"},
        )


def test_router_rejects_unregistered_handler(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    router = LocalRouter(protocol)

    msg = Message(
        message_id="r1", idempotency_key="r-k", correlation_id="c",
        project_id="QLA", task_id="TR", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT,
        timestamp=datetime.now(timezone.utc),
        payload={"objective": "no handler"},
    )

    protocol.accept(msg)

    with pytest.raises(RouterError, match="No handler registered"):
        router.dispatch(msg)


def test_no_unauthorized_side_effects(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    report = orchestrator.run("T16", "local only")

    assert report["outcome"] == "approved"
    assert report["halted"] is False
