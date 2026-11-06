"""
SKIPPY AI TRADER - PHASE 8

Controlled Event Bus Tests.
"""

from skippy.adapters.tradingview.controlled_event_bus import (
    ControlledTradingViewEventBus,
)


def test_controlled_bus_preserves_event_delivery():
    bus = ControlledTradingViewEventBus()
    received = []

    bus.subscribe("test.event", received.append)
    bus.publish("test.event", {"value": 1})

    assert received == [{"value": 1}]
    assert len(bus.history()) == 1


def test_controlled_bus_supports_unsubscribe():
    bus = ControlledTradingViewEventBus()
    received = []

    bus.subscribe("test.event", received.append)
    bus.unsubscribe("test.event", received.append)

    bus.publish("test.event", {"value": 1})

    assert received == []


def test_controlled_ingest_verifies_before_processing():
    bus = ControlledTradingViewEventBus()
    operations = []

    def verify():
        operations.append("VERIFY")

    def ingest(body, token):
        operations.append("INGEST")
        return {"accepted": True}

    result = bus.controlled_ingest(
        verify,
        ingest,
        b"signal",
        "token",
    )

    assert operations == ["VERIFY", "INGEST"]
    assert result == {"accepted": True}


def test_failed_verification_prevents_ingestion():
    bus = ControlledTradingViewEventBus()
    operations = []

    def verify():
        raise RuntimeError("Observer unavailable")

    def ingest(body, token):
        operations.append("INGEST")

    try:
        bus.controlled_ingest(
            verify,
            ingest,
            b"signal",
            "token",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Verification failure was not propagated")

    assert operations == []
