"""
SKIPPY AI TRADER - PHASE 8

Durable Hermes Observation Integration Tests.

LOCAL SIMULATION ONLY.
"""

import json
from datetime import datetime, timezone

import pytest

from skippy.adapters.tradingview.atomic_ingestion import (
    AtomicTradingViewIngestion,
)
from skippy.adapters.tradingview.deduplicator import (
    DuplicateSignalError,
    SignalDeduplicator,
)
from skippy.adapters.tradingview.durable_observer import (
    DurableTradingViewObserver,
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


def test_successful_hermes_observation_completes(tmp_path):
    database = tmp_path / "durable.db"

    ingestion, observer, bus = make_pipeline(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    states = ProcessingStateRegistry(database)
    repository = ObservationRepository(database)

    assert states.get_state(fingerprint) == "COMPLETED"
    assert repository.get(fingerprint) is not None
    assert len(observer.observations) == 1
    assert len(bus.history()) == 1


def test_observation_survives_restart(tmp_path):
    database = tmp_path / "durable.db"

    ingestion, _, _ = make_pipeline(database)

    body = make_body()
    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    reopened = ObservationRepository(database)

    stored = reopened.get(fingerprint)

    assert stored is not None
    assert stored["result"]["decision"] == "WATCH"


def test_hermes_failure_records_failed_state(tmp_path):
    database = tmp_path / "durable.db"

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

    states = ProcessingStateRegistry(database)
    repository = ObservationRepository(database)

    assert states.get_state(fingerprint) == "FAILED"
    assert repository.get(fingerprint) is None
    assert observer.observations == []


def test_duplicate_does_not_repeat_observation(tmp_path):
    database = tmp_path / "durable.db"

    ingestion, observer, _ = make_pipeline(database)

    body = make_body()

    ingestion.ingest(body, TEST_SECRET)

    with pytest.raises(DuplicateSignalError):
        ingestion.ingest(body, TEST_SECRET)

    assert len(observer.observations) == 1


def test_completed_state_survives_restart(tmp_path):
    database = tmp_path / "durable.db"

    ingestion, _, _ = make_pipeline(database)

    body = make_body()

    ingestion.ingest(body, TEST_SECRET)

    fingerprint = fingerprint_for(body)

    reopened_states = ProcessingStateRegistry(database)

    assert reopened_states.get_state(fingerprint) == "COMPLETED"


def test_persistent_database_required():
    with pytest.raises(ValueError):
        DurableTradingViewObserver(
            event_bus=EventBus(),
            database_path=None,
        )