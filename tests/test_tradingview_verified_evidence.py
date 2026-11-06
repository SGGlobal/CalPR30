"""
SKIPPY AI TRADER - PHASE 8

Verified Evidence Retrieval Tests.

LOCAL SIMULATION ONLY.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.deduplicator import SignalDeduplicator
from skippy.adapters.tradingview.observation_repository import (
    ObservationRepository,
)
from skippy.adapters.tradingview.recovery_inspector import (
    RecoveryInspectionError,
    RecoveryInspector,
)
from skippy.adapters.tradingview.validator import validate_signal


LEGACY_FINGERPRINT = "a" * 64


def make_payload():
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def matching_fingerprint(payload: dict) -> str:
    validated = validate_signal(payload)
    return SignalDeduplicator().fingerprint(validated)


def prepare_database(tmp_path):
    database = tmp_path / "verified.db"
    AtomicSignalRegistry(database)
    ObservationRepository(database)
    return database


def test_valid_evidence_passes_verification(tmp_path):
    database = prepare_database(tmp_path)
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    registry.register(fingerprint, evidence=payload)

    inspector = RecoveryInspector(database)
    verified = inspector.get_verified_evidence(fingerprint)

    assert verified is not None
    assert verified["symbol"] == payload["symbol"]
    assert verified["action"] == payload["action"]
    assert verified["price"] == payload["price"]


def test_mismatched_fingerprint_rejected(tmp_path):
    database = prepare_database(tmp_path)

    payload = make_payload()
    # Store evidence under a fingerprint that does not match.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, ?)
            """,
            (
                LEGACY_FINGERPRINT,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )

    inspector = RecoveryInspector(database)

    with pytest.raises(RecoveryInspectionError, match="fingerprint"):
        inspector.get_verified_evidence(LEGACY_FINGERPRINT)


def test_modified_price_detected(tmp_path):
    database = prepare_database(tmp_path)
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    registry.register(fingerprint, evidence=payload)

    # Tamper with the price after storage.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE signals
            SET evidence_json = ?
            WHERE fingerprint = ?
            """,
            (
                json.dumps(
                    {**payload, "price": 1.0},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                fingerprint,
            ),
        )

    inspector = RecoveryInspector(database)

    with pytest.raises(RecoveryInspectionError):
        inspector.get_verified_evidence(fingerprint)


def test_invalid_symbol_rejected(tmp_path):
    database = prepare_database(tmp_path)

    bad = make_payload()
    bad["symbol"] = "INVALID"

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, ?)
            """,
            (
                LEGACY_FINGERPRINT,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(bad, sort_keys=True, separators=(",", ":")),
            ),
        )

    inspector = RecoveryInspector(database)

    with pytest.raises(RecoveryInspectionError):
        inspector.get_verified_evidence(LEGACY_FINGERPRINT)


def test_missing_fields_rejected(tmp_path):
    database = prepare_database(tmp_path)

    incomplete = {"symbol": "BTCUSDT", "action": "BUY"}

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, ?)
            """,
            (
                LEGACY_FINGERPRINT,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(incomplete),
            ),
        )

    inspector = RecoveryInspector(database)

    with pytest.raises(RecoveryInspectionError):
        inspector.get_verified_evidence(LEGACY_FINGERPRINT)


def test_additional_fields_rejected(tmp_path):
    database = prepare_database(tmp_path)

    extra = make_payload()
    extra["unauthorized"] = "must-not-pass"

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, ?)
            """,
            (
                LEGACY_FINGERPRINT,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(extra),
            ),
        )

    inspector = RecoveryInspector(database)

    with pytest.raises(RecoveryInspectionError):
        inspector.get_verified_evidence(LEGACY_FINGERPRINT)


def test_malformed_json_rejected(tmp_path):
    database = prepare_database(tmp_path)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, ?)
            """,
            (
                LEGACY_FINGERPRINT,
                datetime.now(timezone.utc).isoformat(),
                "{not valid json",
            ),
        )

    inspector = RecoveryInspector(database)

    with pytest.raises(RecoveryInspectionError):
        inspector.get_verified_evidence(LEGACY_FINGERPRINT)


def test_historical_null_evidence_supported(tmp_path):
    database = prepare_database(tmp_path)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, NULL)
            """,
            (LEGACY_FINGERPRINT, datetime.now(timezone.utc).isoformat()),
        )

    inspector = RecoveryInspector(database)

    assert inspector.get_verified_evidence(LEGACY_FINGERPRINT) is None


def test_successful_verification_leaves_database_unchanged(tmp_path):
    database = prepare_database(tmp_path)
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    registry.register(fingerprint, evidence=payload)

    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT evidence_json, first_seen FROM signals WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

    inspector = RecoveryInspector(database)
    inspector.get_verified_evidence(fingerprint)

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT evidence_json, first_seen FROM signals WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

    assert before == after


def test_failed_verification_leaves_database_unchanged(tmp_path):
    database = prepare_database(tmp_path)

    payload = make_payload()

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO signals (fingerprint, first_seen, evidence_json)
            VALUES (?, ?, ?)
            """,
            (
                LEGACY_FINGERPRINT,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(payload),
            ),
        )
        before = connection.execute(
            "SELECT evidence_json FROM signals WHERE fingerprint = ?",
            (LEGACY_FINGERPRINT,),
        ).fetchone()

    inspector = RecoveryInspector(database)

    with pytest.raises(RecoveryInspectionError):
        inspector.get_verified_evidence(LEGACY_FINGERPRINT)

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT evidence_json FROM signals WHERE fingerprint = ?",
            (LEGACY_FINGERPRINT,),
        ).fetchone()

    assert before == after


def test_standard_investigation_report_unchanged(tmp_path):
    database = prepare_database(tmp_path)
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    registry.register(fingerprint, evidence=payload)

    inspector = RecoveryInspector(database)
    report = inspector.inspect()

    assert report[0]["fingerprint"] == fingerprint
    assert "evidence" not in report[0]
    assert report[0]["classification"] in {
        "IN_PROGRESS",
        "STALE",
        "COMPLETED",
        "REVIEW_REQUIRED",
        "INCONSISTENT",
    }


def test_interrupted_registration_evidence_inspectable(tmp_path):
    database = prepare_database(tmp_path)
    registry = AtomicSignalRegistry(database)

    payload = make_payload()
    fingerprint = matching_fingerprint(payload)

    # Register (commits) then do not publish — simulated interruption.
    registry.register(fingerprint, evidence=payload)

    inspector = RecoveryInspector(database)
    verified = inspector.get_verified_evidence(fingerprint)

    assert verified is not None
    assert verified["symbol"] == payload["symbol"]

    # Standard report still does not expose evidence.
    report = inspector.inspect()
    assert "evidence" not in report[0]
