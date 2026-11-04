"""
SKIPPY AI TRADER - PHASE 8

Controlled Local Observational Startup.

LOCAL SIMULATION ONLY.
No external networking, automatic retries, or trade execution.
"""

from pathlib import Path

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

    def ingest(self, body: bytes, token: str):
        """
        Accept a local simulated signal only after startup.

        No HTTP listener, brokerage connection,
        or trade execution is provided.
        """

        if not self.ready or self.ingestion is None:
            raise LocalStartupError(
                "Local observational system is not ready"
            )

        return self.ingestion.ingest(body, token)