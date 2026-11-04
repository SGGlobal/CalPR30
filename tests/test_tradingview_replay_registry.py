"""
SKIPPY AI TRADER — PHASE 8

Persistent Replay Registry Tests.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from skippy.adapters.tradingview.replay_registry import (
    PersistentReplayRegistry,
    ReplayRegistryError,
)


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64
TIMESTAMP = "2026-11-04T18:00:00+00:00"


def test_first_registration_accepted(tmp_path):
    registry = PersistentReplayRegistry(tmp_path / "replay.db")

    assert registry.register(FINGERPRINT_A, TIMESTAMP) is True


def test_duplicate_registration_rejected(tmp_path):
    registry = PersistentReplayRegistry(tmp_path / "replay.db")

    assert registry.register(FINGERPRINT_A, TIMESTAMP) is True
    assert registry.register(FINGERPRINT_A, TIMESTAMP) is False


def test_registration_survives_restart(tmp_path):
    database = tmp_path / "replay.db"

    first = PersistentReplayRegistry(database)
    assert first.register(FINGERPRINT_A, TIMESTAMP) is True

    second = PersistentReplayRegistry(database)
    assert second.register(FINGERPRINT_A, TIMESTAMP) is False


def test_different_fingerprints_accepted(tmp_path):
    registry = PersistentReplayRegistry(tmp_path / "replay.db")

    assert registry.register(FINGERPRINT_A, TIMESTAMP) is True
    assert registry.register(FINGERPRINT_B, TIMESTAMP) is True


def test_invalid_fingerprint_rejected(tmp_path):
    registry = PersistentReplayRegistry(tmp_path / "replay.db")

    with pytest.raises(ValueError):
        registry.register("invalid", TIMESTAMP)


def test_missing_database_directory_rejected(tmp_path):
    database = tmp_path / "missing" / "replay.db"

    with pytest.raises(ReplayRegistryError):
        PersistentReplayRegistry(database)


def test_memory_database_rejected():
    with pytest.raises(ValueError):
        PersistentReplayRegistry(":memory:")


def test_concurrent_duplicate_registration(tmp_path):
    database = tmp_path / "replay.db"

    registry = PersistentReplayRegistry(database)

    def register():
        return registry.register(FINGERPRINT_A, TIMESTAMP)

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: register(), range(5)))

    assert results.count(True) == 1
    assert results.count(False) == 4