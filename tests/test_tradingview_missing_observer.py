"""
SKIPPY AI TRADER - PHASE 8

Missing Durable Observer Tests.

LOCAL SIMULATION ONLY.
No networking, automatic retries, or trade execution.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.atomic_ingestion import (
    AtomicTradingViewIngestion,
)
from skippy.adapters.tradingview.deduplicator import (
    DuplicateSignalError,
    SignalDeduplicator,
)
from skippy.adapters.tradingview.investigation_report import (
    InvestigationReport,
)
from skippy.adapters.tradingview.observation_repository import (
    ObservationRepository,
)
from skippy.adapters.tradingview.processing_state import (
    ProcessingStateRegistry,
)
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

def make_ingestion(database):
    bus = EventBus()

    ingestion = AtomicTradingViewIngestion(
        event_bus=bus,
        secret=TEST_SECRET,
        database_path=database,
    )

    # Initialize the observation schema without attaching
    # a durable observer or executing Hermes.
    ObservationRepository(database)

    return ingestion, bus

def test_signal_registered_without_observer(tmp_path):
    database = tmp_path / "missing_observer.db"

    ingestion, bus = make_ingestion(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    assert ProcessingStateRegistry(database).get_state(
        fingerprint
    ) == "RECEIVED"

    assert len(bus.history()) == 1


def test_unobserved_signal_has_no_observation(tmp_path):
    database = tmp_path / "missing_observer.db"

    ingestion, _ = make_ingestion(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    assert ObservationRepository(database).get(
        fingerprint
    ) is None


def test_unobserved_signal_retains_replay_protection(tmp_path):
    database = tmp_path / "missing_observer.db"

    ingestion, bus = make_ingestion(database)

    body = make_body()

    ingestion.ingest(body, TEST_SECRET)

    with pytest.raises(DuplicateSignalError):
        ingestion.ingest(body, TEST_SECRET)

    assert len(bus.history()) == 1


def test_unobserved_signal_reported_in_progress(tmp_path):
    database = tmp_path / "missing_observer.db"

    ingestion, _ = make_ingestion(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    report = InvestigationReport(database).generate()

    assert report["summary"]["IN_PROGRESS"] == 1
    assert report["requires_attention"] == []


def test_unobserved_signal_eventually_reported_stale(tmp_path):
    database = tmp_path / "missing_observer.db"

    ingestion, _ = make_ingestion(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    old_time = (
        datetime.now(timezone.utc) - timedelta(minutes=20)
    ).isoformat()

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE signals
            SET first_seen = ?
            WHERE fingerprint = ?
            """,
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

    report = InvestigationReport(
        database,
        stale_after_seconds=300,
    ).generate()

    assert report["summary"]["STALE"] == 1
    assert len(report["requires_attention"]) == 1

    assert ProcessingStateRegistry(database).get_state(
        fingerprint
    ) == "RECEIVED"


def test_restarted_ingestion_still_rejects_duplicate(tmp_path):
    database = tmp_path / "missing_observer.db"

    first_ingestion, _ = make_ingestion(database)

    body = make_body()
    first_ingestion.ingest(body, TEST_SECRET)

    restarted_ingestion, restarted_bus = make_ingestion(
        database
    )

    with pytest.raises(DuplicateSignalError):
        restarted_ingestion.ingest(body, TEST_SECRET)

    assert restarted_bus.history() == []
