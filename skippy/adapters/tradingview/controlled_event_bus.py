"""
SKIPPY AI TRADER - PHASE 8

Controlled TradingView Event Bus.

LOCAL SIMULATION ONLY.
No external networking, automatic retries, or trade execution.
"""

from threading import RLock

from skippy.kernel.event_bus import EventBus


class ControlledTradingViewEventBus(EventBus):
    """
    Phase 8 event bus with synchronized subscription changes.

    The protected Phase 7 EventBus remains unchanged.
    """

    def __init__(self):
        super().__init__()
        self._control_lock = RLock()

    def subscribe(self, event, callback):
        with self._control_lock:
            return super().subscribe(event, callback)

    def unsubscribe(self, event, callback):
        with self._control_lock:
            return super().unsubscribe(event, callback)

    def publish(self, event, payload=None):
        with self._control_lock:
            return super().publish(event, payload)

    def controlled_ingest(self, verify, ingest, body, token):
        """
        Hold one lock across observer verification,
        signal registration, and synchronous publication.
        """

        with self._control_lock:
            verify()
            return ingest(body, token)
