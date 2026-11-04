"""
SKIPPY AI TRADER — PHASE 8

Timestamp Freshness Tests.
"""

from datetime import datetime, timedelta, timezone

import pytest

from skippy.adapters.tradingview.freshness import (
    SignalFreshnessValidator,
    StaleSignalError,
)
from skippy.adapters.tradingview.validator import validate_signal


def make_signal(timestamp):
    return validate_signal({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": timestamp.isoformat(),
    })


def test_current_signal_accepted():
    now = datetime.now(timezone.utc)
    signal = make_signal(now)

    SignalFreshnessValidator().check(signal, now=now)


def test_old_signal_rejected():
    now = datetime.now(timezone.utc)
    signal = make_signal(now - timedelta(minutes=5))

    with pytest.raises(StaleSignalError):
        SignalFreshnessValidator().check(signal, now=now)


def test_future_signal_rejected():
    now = datetime.now(timezone.utc)
    signal = make_signal(now + timedelta(minutes=5))

    with pytest.raises(StaleSignalError):
        SignalFreshnessValidator().check(signal, now=now)


def test_recent_signal_accepted():
    now = datetime.now(timezone.utc)
    signal = make_signal(now - timedelta(seconds=30))

    SignalFreshnessValidator().check(signal, now=now)


def test_boundary_age_accepted():
    now = datetime.now(timezone.utc)
    signal = make_signal(now - timedelta(seconds=120))

    SignalFreshnessValidator().check(signal, now=now)


def test_boundary_future_accepted():
    now = datetime.now(timezone.utc)
    signal = make_signal(now + timedelta(seconds=10))

    SignalFreshnessValidator().check(signal, now=now)