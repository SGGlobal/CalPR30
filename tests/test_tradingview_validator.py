"""
SKIPPY AI TRADER — Phase 8

Automated tests for TradingView signal validation.
All tests operate locally.
"""

import pytest

from skippy.adapters.tradingview.validator import (
    SignalValidationError,
    validate_signal,
)


def valid_payload():
    return {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": "2026-11-04T18:00:00Z",
    }


def test_valid_signal():
    signal = validate_signal(valid_payload())

    assert signal.symbol == "BTCUSDT"
    assert signal.action == "BUY"
    assert signal.price == 65000.0
    assert signal.timestamp == "2026-11-04T18:00:00+00:00"


def test_missing_field():
    payload = valid_payload()
    del payload["symbol"]

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_invalid_symbol():
    payload = valid_payload()
    payload["symbol"] = "UNKNOWN"

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_invalid_action():
    payload = valid_payload()
    payload["action"] = "EXECUTE"

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_negative_price():
    payload = valid_payload()
    payload["price"] = -500

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_zero_price():
    payload = valid_payload()
    payload["price"] = 0

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_invalid_timestamp():
    payload = valid_payload()
    payload["timestamp"] = "invalid"

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_timestamp_requires_timezone():
    payload = valid_payload()
    payload["timestamp"] = "2026-11-04T18:00:00"

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_boolean_price_rejected():
    payload = valid_payload()
    payload["price"] = True

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_nan_price_rejected():
    payload = valid_payload()
    payload["price"] = float("nan")

    with pytest.raises(SignalValidationError):
        validate_signal(payload)


def test_infinite_price_rejected():
    payload = valid_payload()
    payload["price"] = float("inf")

    with pytest.raises(SignalValidationError):
        validate_signal(payload)