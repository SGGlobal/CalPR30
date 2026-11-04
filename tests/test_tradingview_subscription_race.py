"""
SKIPPY AI TRADER - PHASE 8

Deterministic Observer Readiness Race Test.

LOCAL SIMULATION ONLY.
No external networking, retries, threads, or trade execution.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from skippy.adapters.tradingview.atomic_registry import AtomicSignalRegistry
from skippy.adapters.tradingview.local_startup import (
    LocalTradingViewStartup,
)
from skippy.adapters.tradingview.receiver import (
    TRADINGVIEW_SIGNAL_VALIDATED,
)
from skippy.adapters.tradingview.validator import validate_signal
from skippy.adapters.tradingview.deduplicator import SignalDeduplicator


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


def test_observer_removed_after_readiness_check(tmp_path):
    database = tmp_path / "race.db"

    startup = LocalTradingViewStartup(database, TEST_SECRET)
    startup.start()

    body = make_body()
    fingerprint = fingerprint_for(body)

    original_verify = startup._verify_observer_readiness

    def verify_then_disconnect():
        original_verify()

        startup.event_bus.unsubscribe(
            TRADINGVIEW_SIGNAL_VALIDATED,
            startup.observer._observe,
        )

    with patch.object(
        startup,
        "_verify_observer_readiness",
        side_effect=verify_then_disconnect,
    ):
        startup.ingest(body, TEST_SECRET)

    state = AtomicSignalRegistry(database).get_state(fingerprint)

    assert state == "RECEIVED"
    assert startup.event_bus.history() != []
    assert startup.observer.observations == []
