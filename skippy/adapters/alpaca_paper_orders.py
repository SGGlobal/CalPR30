"""Alpaca paper order adapter and shared submission journal.

The caller supplies an authenticated transport, a durable journal path, and an
order admission callback. Startup and TradingView do not call this adapter.
"""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

PAPER_API = "https://paper-api.alpaca.markets/v2"


class OrderRefused(ValueError):
    """The adapter refused to send an order."""


class OrderUncertain(RuntimeError):
    """The attempt requires read-only reconciliation by client order ID."""


@dataclass(frozen=True)
class PaperMarketOrder:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal

    def payload(self):
        if (
            not isinstance(self.client_order_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.client_order_id)
            or not isinstance(self.symbol, str)
            or not re.fullmatch(r"[A-Z0-9]{1,16}/[A-Z0-9]{1,16}", self.symbol)
            or self.side not in ("buy", "sell")
            or not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= 0
            or abs(self.quantity.as_tuple().exponent) > 18
            or len(self.quantity.as_tuple().digits) > 36
        ):
            raise OrderRefused("Invalid crypto market order")
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": format(self.quantity, "f"),
            "type": "market",
            "time_in_force": "ioc",
        }


class _AlpacaOrders:
    api: str
    journal_table: str

    def __init__(self, journal_path, *, account_id, transport, allow_submit=None):
        if str(journal_path) in ("", ":memory:") or not isinstance(account_id, str) or not account_id:
            raise OrderRefused("A durable journal and an Alpaca account ID are required")
        self.path = journal_path
        self.account_id = account_id
        self.transport = transport
        self.allow_submit = allow_submit
        with closing(self._connect()) as db, db:
            db.execute(
                f"CREATE TABLE IF NOT EXISTS {self.journal_table} ("
                "account TEXT NOT NULL, client_id TEXT NOT NULL, payload TEXT NOT NULL, "
                "broker_id TEXT, PRIMARY KEY (account, client_id))"
            )

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error:
            db.close()
            raise
        return db

    def _admit(self, order):
        if self.allow_submit is None or self.allow_submit(order) is not True:
            raise OrderRefused("Order submission is disabled for this order")

    def _account(self, *, opening=False):
        account = self.transport("GET", f"{self.api}/account", None)
        if not isinstance(account, dict) or account.get("id") != self.account_id:
            raise OrderRefused("Alpaca account identity mismatch")
        if opening and (
            account.get("status") != "ACTIVE"
            or account.get("trading_blocked") is not False
            or account.get("account_blocked") is not False
        ):
            raise OrderRefused("Alpaca account is inactive or restricted")
        return account

    def _stored(self, db, payload):
        return db.execute(
            f"SELECT payload, broker_id FROM {self.journal_table} WHERE account=? AND client_id=?",
            (self.account_id, payload["client_order_id"]),
        ).fetchone()

    def _record(self, payload, broker_order):
        broker_id = _matching_broker_id(payload, broker_order)
        with closing(self._connect()) as db, db:
            updated = db.execute(
                f"UPDATE {self.journal_table} SET broker_id=? WHERE account=? AND client_id=? "
                "AND (broker_id IS NULL OR broker_id=?)",
                (broker_id, self.account_id, payload["client_order_id"], broker_id),
            ).rowcount
            if updated != 1:
                raise OrderUncertain("Alpaca order conflicts with the journal")
        return broker_id

    def submit(self, order) -> str:
        """Return the accepted broker order ID, which does not imply a fill."""
        self._admit(order)
        payload = order.payload()
        self._account(opening=True)
        encoded = json.dumps(payload, sort_keys=True)
        with closing(self._connect()) as db, db:
            reserved = db.execute(
                f"INSERT OR IGNORE INTO {self.journal_table} (account, client_id, payload) "
                "VALUES (?, ?, ?)",
                (self.account_id, payload["client_order_id"], encoded),
            ).rowcount
            if not reserved:
                prior, broker_id = self._stored(db, payload)
                if prior != encoded:
                    raise OrderRefused("Client order ID is already bound to a different order")
                if broker_id is None:
                    raise OrderUncertain("Alpaca order already reserved; reconcile before proceeding")
                return broker_id

        try:
            self._admit(order)
            order_response = self.transport("POST", f"{self.api}/orders", payload.copy())
            return self._record(payload, order_response)
        except Exception:
            raise OrderUncertain("Alpaca order outcome unverified; reconcile by client ID") from None

    def reconcile(self, client_order_id: str) -> str:
        """Read back a previously reserved order without sending another order."""
        with closing(self._connect()) as db:
            row = self._stored(db, {"client_order_id": client_order_id})
        if row is None:
            raise OrderRefused("Client order ID has no local reservation")
        payload = json.loads(row[0])
        self._account()
        query = urlencode({"client_order_id": client_order_id})
        try:
            order_response = self.transport(
                "GET", f"{self.api}/orders:by_client_order_id?{query}", None
            )
            return self._record(payload, order_response)
        except Exception:
            raise OrderUncertain("Alpaca order could not be reconciled; reservation retained") from None


class AlpacaPaperOrders(_AlpacaOrders):
    api = PAPER_API
    journal_table = "paper_orders"


def _matching_broker_id(payload, reply):
    if not isinstance(reply, dict):
        raise OrderUncertain("Alpaca broker did not return an order")
    broker_id = reply.get("id")
    symbol = reply.get("symbol")
    quantity = reply.get("qty")
    if (
        not isinstance(broker_id, str) or not broker_id
        or reply.get("client_order_id") != payload["client_order_id"]
        or not isinstance(symbol, str)
        or symbol not in (payload["symbol"], payload["symbol"].replace("/", ""))
        or any(reply.get(field) != payload[field] for field in ("side", "type", "time_in_force"))
        or not isinstance(quantity, str)
        or Decimal(quantity) != Decimal(payload["qty"])
        or (
            "limit_price" in payload
            and (not isinstance(reply.get("limit_price"), str)
                 or Decimal(reply["limit_price"]) != Decimal(payload["limit_price"]))
        )
    ):
        raise OrderUncertain("Alpaca broker order does not match the request")
    return broker_id
