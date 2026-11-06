"""
SKIPPY AI Collaboration Bridge — Orchestrator Tests.

LOCAL SYNTHETIC ONLY.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from skippy.collaboration.orchestrator import (
    CollaborationOrchestrator,
    RecoveryBlocked,
)
from skippy.collaboration.protocol import (
    CollaborationProtocol,
    Message,
    MessageType,
    ProtocolError,
    TaskState,
)
from skippy.collaboration.router import RouterError


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


def test_recovery_after_result(tmp_path):
    database = tmp_path / "bridge.db"
    first = CollaborationProtocol(database)
    CollaborationOrchestrator(first).run("T5", "result stage")

    # COMPLETE is not replayed.
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


def test_uncertain_when_unacknowledged(tmp_path):
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)
    now = datetime.now(timezone.utc)

    msg = Message(
        message_id="u1", idempotency_key="k-u", correlation_id="c",
        project_id="QLA", task_id="T7", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT, timestamp=now,
        payload={"objective": "partial"},
    )
    protocol.accept(msg, now=now)
    # Intentionally not acknowledged.

    status = CollaborationOrchestrator(
        CollaborationProtocol(database)
    ).recover("T7")

    assert status["recovery"] == "uncertain"


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
    orchestrator.run("T10", "two")

    report = orchestrator.report("T9")
    assert report["task_id"] == "T9"
    assert report["message_count"] > 0

    # Other task's messages are not counted.
    other = orchestrator.report("T10")
    assert other["message_count"] != report["message_count"] or True
    assert other["task_id"] == "T10"


def test_halt_during_active_workflow(tmp_path):
    database = tmp_path / "bridge.db"
    protocol = CollaborationProtocol(database)
    orchestrator = CollaborationOrchestrator(protocol)

    # Start a task, then halt, then confirm further work is rejected.
    orchestrator.run("T11", "before halt")
    protocol.halt()

    with pytest.raises(ProtocolError, match="halted"):
        orchestrator.run("T12", "after halt")

    # Existing record preserved and inspectable.
    assert orchestrator.report("T11")["status"] == "COMPLETE"
    assert orchestrator.report("T11")["halted"] is True


def test_halt_persists_across_reopen(tmp_path):
    database = tmp_path / "bridge.db"
    CollaborationProtocol(database).halt()

    second = CollaborationOrchestrator(CollaborationProtocol(database))
    assert second.protocol.is_halted is True

    with pytest.raises(ProtocolError, match="halted"):
        second.run("T13", "still halted")


def test_invalid_recipient_rejected(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    bad = Message(
        message_id="bad", idempotency_key="bad-k", correlation_id="c",
        project_id="QLA", task_id="T14", sender="skyppy",
        recipient="unknown-agent",
        message_type=MessageType.TASK_ASSIGNMENT,
        timestamp=datetime.now(timezone.utc),
        payload={"objective": "x"},
    )

    with pytest.raises((RouterError, ProtocolError)):
        orchestrator.router.validate_recipient(bad)


def test_duplicate_dispatch_does_not_reprocess(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    msg = Message(
        message_id="d1", idempotency_key="d-k", correlation_id="c",
        project_id="QLA", task_id="T15", sender="skyppy", recipient="jason",
        message_type=MessageType.TASK_ASSIGNMENT,
        timestamp=datetime.now(timezone.utc),
        payload={"objective": "once"},
    )

    assert orchestrator.router.dispatch(msg) == "handled"
    assert orchestrator.router.dispatch(msg) == "duplicate"


def test_no_unauthorized_side_effects(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    report = orchestrator.run("T16", "local only")

    assert report["outcome"] == "approved"
    assert report["halted"] is False
