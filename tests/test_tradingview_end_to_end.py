"""
SKIPPY AI TRADER - PHASE 8

End-to-End Observational Lifecycle Tests.

LOCAL SIMULATION ONLY.
No external networking, automatic retries, or trade execution.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.atomic_ingestion import (
    AtomicTradingViewIngestion,
)
from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.deduplicator import (
    DuplicateSignalError,
    SignalDeduplicator,
)
from skippy.adapters.tradingview.durable_observer import (
    DurableTradingViewObserver,
)
from skippy.adapters.tradingview.investigation_report import InvestigationReport
from skippy.adapters.tradingview.observation_repository import ObservationRepository
from skippy.adapters.tradingview.processing_state import ProcessingStateRegistry
from skippy.adapters.tradingview.validator import validate_signal
from skippy.kernel.event_bus import EventBus


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def make_body():
    return json.dumps({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")


def fingerprint_for(body):
    validated = validate_signal(json.loads(body))
    return SignalDeduplicator().fingerprint(validated)


def make_pipeline(database, hermes=None):
    bus = EventBus()

    observer = DurableTradingViewObserver(
        event_bus=bus,
        database_path=database,
        hermes=hermes,
    )

    ingestion = AtomicTradingViewIngestion(
        event_bus=bus,
        secret=TEST_SECRET,
        database_path=database,
    )

    return ingestion, observer, bus


def test_end_to_end_successful_observation(tmp_path):
    database = tmp_path / "lifecycle.db"

    ingestion, observer, bus = make_pipeline(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    report = InvestigationReport(database).generate()
    repository = ObservationRepository(database)

    assert len(bus.history()) == 1
    assert len(observer.observations) == 1

    assert repository.get(fingerprint) is not None

    assert report["total_signals"] == 1
    assert report["summary"]["COMPLETED"] == 1
    assert report["requires_attention"] == []


def test_end_to_end_hermes_failure(tmp_path):
    database = tmp_path / "lifecycle.db"

    class FailingHermes:
        def observe(self, signal):
            raise RuntimeError("Simulated Hermes failure")

    ingestion, observer, _ = make_pipeline(
        database,
        hermes=FailingHermes(),
    )

    body = make_body()

    with pytest.raises(RuntimeError, match="Simulated Hermes failure"):
        ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    report = InvestigationReport(database).generate()

    assert ProcessingStateRegistry(database).get_state(fingerprint) == "FAILED"
    assert ObservationRepository(database).get(fingerprint) is None

    assert report["summary"]["REVIEW_REQUIRED"] == 1
    assert len(report["requires_attention"]) == 1
    assert observer.observations == []


def test_duplicate_signal_does_not_repeat_processing(tmp_path):
    database = tmp_path / "lifecycle.db"

    ingestion, observer, bus = make_pipeline(database)

    body = make_body()

    ingestion.ingest(body, TEST_SECRET)

    with pytest.raises(DuplicateSignalError):
        ingestion.ingest(body, TEST_SECRET)

    report = InvestigationReport(database).generate()

    assert len(observer.observations) == 1
    assert len(bus.history()) == 1
    assert report["total_signals"] == 1
    assert report["summary"]["COMPLETED"] == 1


def test_interrupted_processing_is_reported_stale(tmp_path):
    database = tmp_path / "lifecycle.db"

    fingerprint = "a" * 64

    registry = AtomicSignalRegistry(database)
    assert registry.register(fingerprint) is True

    states = ProcessingStateRegistry(database)
    states.transition(fingerprint, "PROCESSING")

    old_time = (
        datetime.now(timezone.utc) - timedelta(minutes=20)
    ).isoformat()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE signals SET first_seen = ? WHERE fingerprint = ?",
            (old_time, fingerprint),
        )

        connection.execute(
            """
            UPDATE processing_states
            SET updated_at = ?
            WHERE fingerprint = ?
            """,
            (old_time, fingerprint),
        )

    ObservationRepository(database)

    report = InvestigationReport(
        database,
        stale_after_seconds=300,
    ).generate()

    assert states.get_state(fingerprint) == "PROCESSING"
    assert report["summary"]["STALE"] == 1
    assert len(report["requires_attention"]) == 1


def test_investigation_report_preserves_completed_records(tmp_path):
    database = tmp_path / "lifecycle.db"

    ingestion, _, _ = make_pipeline(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    states = ProcessingStateRegistry(database)
    repository = ObservationRepository(database)

    before_state = states.get_state(fingerprint)
    before_observation = repository.get(fingerprint)

    reporter = InvestigationReport(database)

    first_report = reporter.generate()
    second_report = reporter.generate()

    after_state = states.get_state(fingerprint)
    after_observation = repository.get(fingerprint)

    assert before_state == after_state == "COMPLETED"
    assert before_observation == after_observation

    assert first_report["summary"]["COMPLETED"] == 1
    assert second_report["summary"]["COMPLETED"] == 1
