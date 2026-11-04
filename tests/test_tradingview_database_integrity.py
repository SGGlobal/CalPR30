"""
SKIPPY AI TRADER - PHASE 8

Database Integrity Tests.

LOCAL SIMULATION ONLY.
No database repair, networking, or trade execution.
"""

import sqlite3

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.investigation_report import InvestigationReport
from skippy.adapters.tradingview.observation_repository import ObservationRepository


FINGERPRINT = "a" * 64


def create_database(tmp_path):
    database = tmp_path / "integrity.db"

    AtomicSignalRegistry(database)
    ObservationRepository(database)

    return database


def register_signal(database):
    registry = AtomicSignalRegistry(database)

    assert registry.register(FINGERPRINT) is True


def inspect(database):
    return InvestigationReport(database).generate()


def test_missing_processing_state_detected(tmp_path):
    database = create_database(tmp_path)
    register_signal(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM processing_states WHERE fingerprint = ?",
            (FINGERPRINT,),
        )

    report = inspect(database)

    assert report["summary"]["INCONSISTENT"] == 1
    assert len(report["requires_attention"]) == 1


def test_missing_replay_record_detected(tmp_path):
    database = create_database(tmp_path)
    register_signal(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM signals WHERE fingerprint = ?",
            (FINGERPRINT,),
        )

    report = inspect(database)

    assert report["summary"]["INCONSISTENT"] == 1


def test_observation_without_completion_detected(tmp_path):
    database = create_database(tmp_path)
    register_signal(database)

    ObservationRepository(database).save(
        FINGERPRINT,
        {"decision": "WATCH"},
    )

    report = inspect(database)

    assert report["summary"]["INCONSISTENT"] == 1


def test_completed_without_observation_detected(tmp_path):
    database = create_database(tmp_path)
    register_signal(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE processing_states
            SET state = 'COMPLETED'
            WHERE fingerprint = ?
            """,
            (FINGERPRINT,),
        )

    report = inspect(database)

    assert report["summary"]["INCONSISTENT"] == 1


def test_orphan_observation_detected(tmp_path):
    database = create_database(tmp_path)

    ObservationRepository(database).save(
        FINGERPRINT,
        {"decision": "WATCH"},
    )

    report = inspect(database)

    assert report["summary"]["INCONSISTENT"] == 1
    assert report["total_signals"] == 1


def test_inspection_does_not_repair_missing_state(tmp_path):
    database = create_database(tmp_path)
    register_signal(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM processing_states WHERE fingerprint = ?",
            (FINGERPRINT,),
        )

    first_report = inspect(database)
    second_report = inspect(database)

    assert first_report["summary"]["INCONSISTENT"] == 1
    assert second_report["summary"]["INCONSISTENT"] == 1

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT state
            FROM processing_states
            WHERE fingerprint = ?
            """,
            (FINGERPRINT,),
        ).fetchone()

    assert row is None
