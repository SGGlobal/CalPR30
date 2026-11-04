"""
SKIPPY AI TRADER — PHASE 8

Persistent Processing-State Tests.
"""

import pytest

from skippy.adapters.tradingview.processing_state import (
    ProcessingStateError,
    ProcessingStateRegistry,
)


FINGERPRINT = "a" * 64


def test_register_creates_received_state(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    assert registry.register(FINGERPRINT) is True
    assert registry.get_state(FINGERPRINT) == "RECEIVED"


def test_duplicate_registration_rejected(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    assert registry.register(FINGERPRINT) is True
    assert registry.register(FINGERPRINT) is False


def test_processing_transition(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    registry.register(FINGERPRINT)
    registry.transition(FINGERPRINT, "PROCESSING")

    assert registry.get_state(FINGERPRINT) == "PROCESSING"


def test_completed_transition(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    registry.register(FINGERPRINT)
    registry.transition(FINGERPRINT, "PROCESSING")
    registry.transition(FINGERPRINT, "COMPLETED")

    assert registry.get_state(FINGERPRINT) == "COMPLETED"


def test_failed_transition(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    registry.register(FINGERPRINT)
    registry.transition(FINGERPRINT, "PROCESSING")
    registry.transition(FINGERPRINT, "FAILED")

    assert registry.get_state(FINGERPRINT) == "FAILED"


def test_failed_moves_to_review_required(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    registry.register(FINGERPRINT)
    registry.transition(FINGERPRINT, "PROCESSING")
    registry.transition(FINGERPRINT, "FAILED")
    registry.transition(FINGERPRINT, "REVIEW_REQUIRED")

    assert registry.get_state(FINGERPRINT) == "REVIEW_REQUIRED"


def test_completed_state_cannot_restart(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    registry.register(FINGERPRINT)
    registry.transition(FINGERPRINT, "PROCESSING")
    registry.transition(FINGERPRINT, "COMPLETED")

    with pytest.raises(ProcessingStateError):
        registry.transition(FINGERPRINT, "PROCESSING")


def test_unknown_signal_rejected(tmp_path):
    registry = ProcessingStateRegistry(tmp_path / "states.db")

    with pytest.raises(ProcessingStateError):
        registry.transition(FINGERPRINT, "PROCESSING")


def test_state_survives_restart(tmp_path):
    database = tmp_path / "states.db"

    first = ProcessingStateRegistry(database)
    first.register(FINGERPRINT)
    first.transition(FINGERPRINT, "PROCESSING")

    second = ProcessingStateRegistry(database)

    assert second.get_state(FINGERPRINT) == "PROCESSING"


def test_missing_directory_rejected(tmp_path):
    with pytest.raises(ProcessingStateError):
        ProcessingStateRegistry(tmp_path / "missing" / "states.db")