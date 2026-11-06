"""
SKIPPY AI Collaboration Bridge — Orchestrator Tests.

LOCAL SYNTHETIC ONLY.
"""

from datetime import datetime, timezone

import pytest

from skippy.collaboration.orchestrator import (
    CollaborationOrchestrator,
    OrchestratorError,
)
from skippy.collaboration.protocol import (
    CollaborationProtocol,
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


def test_message_routing_and_ack(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    orchestrator.run("T3", "route me")

    assert protocol.unacknowledged() == []


def test_duplicate_delivery_is_safe(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    orchestrator.run("T4", "once")

    # Second run of the same task is rejected by the protocol.
    with pytest.raises(ProtocolError):
        orchestrator.run("T4", "twice")


def test_invalid_recipient_rejected(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    with pytest.raises(RouterError):
        orchestrator.router.deliver(
            type("M", (), {
                "message_id": "x",
                "recipient": "unknown",
                "message_type": type("T", (), {"value": "TASK_ASSIGNMENT"})(),
            })()
        )


def test_halt_prevents_new_work(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    protocol.halt()

    with pytest.raises(ProtocolError, match="halted"):
        orchestrator.run("T5", "should fail")


def test_halt_persists_across_reopen(tmp_path):
    database = tmp_path / "bridge.db"

    first = CollaborationProtocol(database)
    first.halt()

    second = CollaborationProtocol(database)
    orchestrator = CollaborationOrchestrator(second)

    assert second.is_halted is True
    with pytest.raises(ProtocolError, match="halted"):
        orchestrator.run("T6", "still halted")


def test_persistence_and_recovery(tmp_path):
    database = tmp_path / "bridge.db"

    first = CollaborationProtocol(database)
    orchestrator = CollaborationOrchestrator(first)
    orchestrator.run("T7", "persist me")

    second = CollaborationProtocol(database)
    recovered = CollaborationOrchestrator(second)

    assert recovered.protocol.get_state("T7") is TaskState.COMPLETE
    assert recovered.report("T7")["status"] == "COMPLETE"


def test_mission_report_is_read_only(tmp_path):
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    orchestrator.run("T8", "report")
    text = orchestrator.to_json("T8")

    assert "T8" in text
    assert "COMPLETE" in text
    assert "secret" not in text.lower()


def test_no_external_side_effects(tmp_path):
    """
    The orchestrator must not import or call external AI clients.
    This test only confirms the workflow completes locally.
    """
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    orchestrator = CollaborationOrchestrator(protocol)

    report = orchestrator.run("T9", "local only")

    assert report["outcome"] == "approved"
    assert report["halted"] is False
