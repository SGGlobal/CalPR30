"""
SKIPPY AI TRADER - PHASE 8

Controlled Local Observational Startup.

LOCAL SIMULATION ONLY.
No external networking, automatic retries, or trade execution.
"""

from pathlib import Path

from skippy.adapters.tradingview.receiver import (
    TRADINGVIEW_SIGNAL_VALIDATED,
)
from skippy.adapters.tradingview.atomic_ingestion import (
    AtomicTradingViewIngestion,
)
from skippy.adapters.tradingview.database_initializer import (
    TradingViewDatabaseInitializer,
)
from skippy.adapters.tradingview.durable_observer import (
    DurableTradingViewObserver,
)
from skippy.kernel.event_bus import EventBus
from skippy.reasoning.hermes import Hermes


class LocalStartupError(RuntimeError):
    """Raised when local observational startup fails."""


class LocalTradingViewStartup:
    def __init__(
        self,
        database_path: str | Path,
        secret: str,
        hermes: Hermes | None = None,
    ):
        self.database_path = database_path
        self.secret = secret
        self.hermes = hermes

        self.event_bus = None
        self.observer = None
        self.ingestion = None
        self.ready = False

    def start(self) -> None:
        """
        Initialize the database, register Hermes,
        and prepare local simulated ingestion.

        Ingestion is exposed only after setup succeeds.
        """

        if self.ready:
            raise LocalStartupError(
                "Local observational startup is already complete"
            )

        try:
            initializer = TradingViewDatabaseInitializer(
                self.database_path
            )

            initializer.initialize()
            initializer.verify()

            bus = EventBus()

            observer = DurableTradingViewObserver(
                event_bus=bus,
                database_path=self.database_path,
                hermes=self.hermes,
            )

            ingestion = AtomicTradingViewIngestion(
                event_bus=bus,
                secret=self.secret,
                database_path=self.database_path,
            )

        except Exception as exc:
            self.ready = False
            self.event_bus = None
            self.observer = None
            self.ingestion = None

            raise LocalStartupError(
                "Local observational startup failed"
            ) from exc

        self.event_bus = bus
        self.observer = observer
        self.ingestion = ingestion
        self.ready = True

    def _verify_observer_readiness(self) -> None:
        """
        Verify that exactly one designated durable observer
        is subscribed to the TradingView event.

        LOCAL SIMULATION ONLY.
        """

        if (
            not self.ready
            or self.event_bus is None
            or self.observer is None
            or self.ingestion is None
        ):
            raise LocalStartupError(
                "Local observational system is not ready"
            )

        subscribers = self.event_bus._subscribers.get(
            TRADINGVIEW_SIGNAL_VALIDATED,
            [],
        )

        expected_callback = self.observer._observe

        if (
            len(subscribers) != 1
            or subscribers[0] != expected_callback
        ):
            raise LocalStartupError(
                "Durable observer readiness verification failed"
            )

    def ingest(self, body: bytes, token: str):
        """
        Accept local simulated signals only when the
        designated durable observer is registered exactly once.
        """

        self._verify_observer_readiness()

        return self.ingestion.ingest(body, token)