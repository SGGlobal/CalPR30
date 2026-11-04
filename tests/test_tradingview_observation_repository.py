"""
SKIPPY AI TRADER - PHASE 8

Persistent Observation Repository Tests.
"""

import pytest

from skippy.adapters.tradingview.observation_repository import (
    ObservationRepository,
    ObservationRepositoryError,
)


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64


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


def test_observation_saved(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    assert repository.save(FINGERPRINT_A, make_observation()) is True

    stored = repository.get(FINGERPRINT_A)

    assert stored["fingerprint"] == FINGERPRINT_A
    assert stored["result"]["decision"] == "WATCH"


def test_duplicate_observation_rejected(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    assert repository.save(FINGERPRINT_A, make_observation()) is True
    assert repository.save(FINGERPRINT_A, make_observation()) is False


def test_existing_observation_not_overwritten(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    repository.save(FINGERPRINT_A, make_observation())

    replacement = make_observation()
    replacement["decision"] = "BUY"

    assert repository.save(FINGERPRINT_A, replacement) is False

    stored = repository.get(FINGERPRINT_A)

    assert stored["result"]["decision"] == "WATCH"


def test_observation_survives_restart(tmp_path):
    database = tmp_path / "observations.db"

    first = ObservationRepository(database)
    first.save(FINGERPRINT_A, make_observation())

    second = ObservationRepository(database)

    stored = second.get(FINGERPRINT_A)

    assert stored["result"]["decision"] == "WATCH"


def test_multiple_observations_saved(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    assert repository.save(FINGERPRINT_A, make_observation()) is True
    assert repository.save(FINGERPRINT_B, make_observation()) is True


def test_invalid_fingerprint_rejected(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    with pytest.raises(ValueError):
        repository.save("invalid", make_observation())


def test_empty_observation_rejected(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    with pytest.raises(ValueError):
        repository.save(FINGERPRINT_A, {})


def test_nonserializable_observation_rejected(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    with pytest.raises(ValueError):
        repository.save(
            FINGERPRINT_A,
            {"invalid": {1, 2, 3}},
        )


def test_nan_observation_rejected(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    with pytest.raises(ValueError):
        repository.save(
            FINGERPRINT_A,
            {"confidence": float("nan")},
        )


def test_unknown_observation_returns_none(tmp_path):
    repository = ObservationRepository(tmp_path / "observations.db")

    assert repository.get(FINGERPRINT_A) is None


def test_missing_directory_rejected(tmp_path):
    with pytest.raises(ObservationRepositoryError):
        ObservationRepository(
            tmp_path / "missing" / "observations.db"
        )


def test_memory_database_rejected():
    with pytest.raises(ValueError):
        ObservationRepository(":memory:")