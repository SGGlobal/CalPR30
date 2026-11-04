"""
SKIPPY AI TRADER - PHASE 8

Transactional Observation Completion Tests.
"""

import sqlite3

import pytest

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.completion_coordinator import (
    CompletionError,
    ObservationCompletionCoordinator,
)
from skippy.adapters.tradingview.observation_repository import ObservationRepository
from skippy.adapters.tradingview.processing_state import ProcessingStateRegistry


FINGERPRINT = "a" * 64


def make_observation():
    return {
        "observation": {
            "symbol": "BTCUSDT",
            "action": "BUY",
            "price": 65000,
        },
        "confidence": 0,
        "decision": "WATCH",
    }


def prepare_processing_signal(database):
    registry = AtomicSignalRegistry(database)

    assert registry.register(FINGERPRINT) is True

    states = ProcessingStateRegistry(database)
    states.transition(FINGERPRINT, "PROCESSING")


def test_observation_and_completion_saved_together(tmp_path):
    database = tmp_path / "completion.db"
    prepare_processing_signal(database)

    coordinator = ObservationCompletionCoordinator(database)
    coordinator.complete(FINGERPRINT, make_observation())

    states = ProcessingStateRegistry(database)
    observations = ObservationRepository(database)

    assert states.get_state(FINGERPRINT) == "COMPLETED"
    assert observations.get(FINGERPRINT)["result"]["decision"] == "WATCH"


def test_completion_survives_restart(tmp_path):
    database = tmp_path / "completion.db"
    prepare_processing_signal(database)

    coordinator = ObservationCompletionCoordinator(database)
    coordinator.complete(FINGERPRINT, make_observation())

    reopened_states = ProcessingStateRegistry(database)
    reopened_observations = ObservationRepository(database)

    assert reopened_states.get_state(FINGERPRINT) == "COMPLETED"
    assert reopened_observations.get(FINGERPRINT) is not None


def test_received_signal_cannot_complete(tmp_path):
    database = tmp_path / "completion.db"

    registry = AtomicSignalRegistry(database)
    registry.register(FINGERPRINT)

    coordinator = ObservationCompletionCoordinator(database)

    with pytest.raises(CompletionError):
        coordinator.complete(FINGERPRINT, make_observation())

    assert ObservationRepository(database).get(FINGERPRINT) is None


def test_unknown_signal_cannot_complete(tmp_path):
    database = tmp_path / "completion.db"
    coordinator = ObservationCompletionCoordinator(database)

    with pytest.raises(CompletionError):
        coordinator.complete(FINGERPRINT, make_observation())


def test_duplicate_completion_rejected(tmp_path):
    database = tmp_path / "completion.db"
    prepare_processing_signal(database)

    coordinator = ObservationCompletionCoordinator(database)
    coordinator.complete(FINGERPRINT, make_observation())

    with pytest.raises(CompletionError):
        coordinator.complete(FINGERPRINT, make_observation())

    assert ProcessingStateRegistry(database).get_state(FINGERPRINT) == "COMPLETED"


def test_invalid_observation_rejected(tmp_path):
    database = tmp_path / "completion.db"
    prepare_processing_signal(database)

    coordinator = ObservationCompletionCoordinator(database)

    with pytest.raises(ValueError):
        coordinator.complete(FINGERPRINT, {"invalid": {1, 2, 3}})

    assert ProcessingStateRegistry(database).get_state(FINGERPRINT) == "PROCESSING"
    assert ObservationRepository(database).get(FINGERPRINT) is None


def test_failed_observation_insert_rolls_back_completion(tmp_path):
    database = tmp_path / "completion.db"
    prepare_processing_signal(database)

    coordinator = ObservationCompletionCoordinator(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_observation
            BEFORE INSERT ON observations
            BEGIN
                SELECT RAISE(ABORT, 'simulated observation failure');
            END;
            """
        )

    with pytest.raises(CompletionError):
        coordinator.complete(FINGERPRINT, make_observation())

    states = ProcessingStateRegistry(database)
    observations = ObservationRepository(database)

    assert states.get_state(FINGERPRINT) == "PROCESSING"
    assert observations.get(FINGERPRINT) is None


def test_failed_state_update_rolls_back_observation(tmp_path):
    database = tmp_path / "completion.db"
    prepare_processing_signal(database)

    coordinator = ObservationCompletionCoordinator(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_completion
            BEFORE UPDATE ON processing_states
            BEGIN
                SELECT RAISE(ABORT, 'simulated state update failure');
            END;
            """
        )

    with pytest.raises(CompletionError):
        coordinator.complete(FINGERPRINT, make_observation())

    states = ProcessingStateRegistry(database)
    observations = ObservationRepository(database)

    assert states.get_state(FINGERPRINT) == "PROCESSING"
    assert observations.get(FINGERPRINT) is None


def test_missing_replay_record_rejected(tmp_path):
    database = tmp_path / "completion.db"
    prepare_processing_signal(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM signals WHERE fingerprint = ?",
            (FINGERPRINT,),
        )

    coordinator = ObservationCompletionCoordinator(database)

    with pytest.raises(CompletionError):
        coordinator.complete(FINGERPRINT, make_observation())

    assert ObservationRepository(database).get(FINGERPRINT) is None


def test_invalid_fingerprint_rejected(tmp_path):
    database = tmp_path / "completion.db"
    coordinator = ObservationCompletionCoordinator(database)

    with pytest.raises(ValueError):
        coordinator.complete("invalid", make_observation())