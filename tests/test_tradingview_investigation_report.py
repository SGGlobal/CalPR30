"""
SKIPPY AI TRADER - PHASE 8

Manual Investigation Report Tests.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.investigation_report import InvestigationReport
from skippy.adapters.tradingview.processing_state import ProcessingStateRegistry


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64
FINGERPRINT_C = "c" * 64


def create_database(tmp_path):
    database = tmp_path / "investigation.db"

    AtomicSignalRegistry(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                fingerprint TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )

    return database


def test_empty_database_report(tmp_path):
    database = create_database(tmp_path)

    report = InvestigationReport(database).generate()

    assert report["total_signals"] == 0
    assert report["requires_attention"] == []
    assert all(
        count == 0
        for count in report["summary"].values()
    )


def test_recent_signal_counted_in_progress(tmp_path):
    database = create_database(tmp_path)
    AtomicSignalRegistry(database).register(FINGERPRINT_A)

    report = InvestigationReport(database).generate()

    assert report["summary"]["IN_PROGRESS"] == 1
    assert report["requires_attention"] == []


def test_failed_signal_requires_attention(tmp_path):
    database = create_database(tmp_path)

    AtomicSignalRegistry(database).register(FINGERPRINT_A)

    states = ProcessingStateRegistry(database)
    states.transition(FINGERPRINT_A, "PROCESSING")
    states.transition(FINGERPRINT_A, "FAILED")

    report = InvestigationReport(database).generate()

    assert report["summary"]["REVIEW_REQUIRED"] == 1
    assert len(report["requires_attention"]) == 1


def test_stale_signal_requires_attention(tmp_path):
    database = create_database(tmp_path)

    AtomicSignalRegistry(database).register(FINGERPRINT_A)

    old_time = (
        datetime.now(timezone.utc) - timedelta(minutes=20)
    ).isoformat()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE signals SET first_seen = ? WHERE fingerprint = ?",
            (old_time, FINGERPRINT_A),
        )
        connection.execute(
            "UPDATE processing_states SET updated_at = ? WHERE fingerprint = ?",
            (old_time, FINGERPRINT_A),
        )

    report = InvestigationReport(database).generate()

    assert report["summary"]["STALE"] == 1
    assert len(report["requires_attention"]) == 1


def test_inconsistent_record_requires_attention(tmp_path):
    database = create_database(tmp_path)

    AtomicSignalRegistry(database).register(FINGERPRINT_A)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM signals WHERE fingerprint = ?",
            (FINGERPRINT_A,),
        )

    report = InvestigationReport(database).generate()

    assert report["summary"]["INCONSISTENT"] == 1
    assert len(report["requires_attention"]) == 1


def test_multiple_signal_summary(tmp_path):
    database = create_database(tmp_path)

    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT_A)
    registry.register(FINGERPRINT_B)
    registry.register(FINGERPRINT_C)

    states = ProcessingStateRegistry(database)
    states.transition(FINGERPRINT_B, "PROCESSING")
    states.transition(FINGERPRINT_B, "FAILED")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM signals WHERE fingerprint = ?",
            (FINGERPRINT_C,),
        )

    report = InvestigationReport(database).generate()

    assert report["total_signals"] == 3
    assert report["summary"]["IN_PROGRESS"] == 1
    assert report["summary"]["REVIEW_REQUIRED"] == 1
    assert report["summary"]["INCONSISTENT"] == 1
    assert len(report["requires_attention"]) == 2


def test_json_serialization(tmp_path):
    database = create_database(tmp_path)

    report = InvestigationReport(database).generate()
    serialized = InvestigationReport.to_json(report)

    decoded = json.loads(serialized)

    assert decoded["total_signals"] == 0


def test_report_generation_does_not_change_state(tmp_path):
    database = create_database(tmp_path)

    AtomicSignalRegistry(database).register(FINGERPRINT_A)

    before = ProcessingStateRegistry(database).get_state(FINGERPRINT_A)

    InvestigationReport(database).generate()

    after = ProcessingStateRegistry(database).get_state(FINGERPRINT_A)

    assert before == after == "RECEIVED"


def test_report_contains_timestamp(tmp_path):
    database = create_database(tmp_path)

    now = datetime.now(timezone.utc)

    report = InvestigationReport(database).generate(now=now)

    assert report["generated_at"] == now.isoformat()


def test_invalid_inspection_time_rejected(tmp_path):
    database = create_database(tmp_path)

    with pytest.raises(ValueError):
        InvestigationReport(database).generate(
            datetime(2026, 11, 4)
        )