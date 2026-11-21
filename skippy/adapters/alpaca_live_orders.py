"""Opt-in Alpaca live crypto limit orders with explicit, expiring approval."""

import http.client
import json
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from .alpaca_paper_orders import PaperMarketOrder, OrderRefused, _AlpacaOrders

LIVE_HOST = "api.alpaca.markets"
LIVE_API = f"https://{LIVE_HOST}/v2"
MAX_RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True)
class LiveLimitOrder:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal

    def payload(self):
        if (
            not isinstance(self.limit_price, Decimal)
            or not self.limit_price.is_finite()
            or self.limit_price <= 0
            or abs(self.limit_price.as_tuple().exponent) > 18
            or len(self.limit_price.as_tuple().digits) > 36
        ):
            raise OrderRefused("Invalid crypto limit price")
        payload = PaperMarketOrder(
            self.client_order_id, self.symbol, self.side, self.quantity
        ).payload()
        payload.update(type="limit", limit_price=format(self.limit_price, "f"))
        return payload


@dataclass(frozen=True)
class LiveOrderApproval:
    account_id: str
    order: LiveLimitOrder
    expires_at: datetime


class AlpacaLiveOrders(_AlpacaOrders):
    api = LIVE_API
    journal_table = "live_orders"

    def __init__(
        self, journal_path, *, account_id, transport, live_enabled=False,
        approval=None, halt_clear=None,
    ):
        super().__init__(journal_path, account_id=account_id, transport=transport)
        self.live_enabled = live_enabled
        self.approval = approval
        self.halt_clear = halt_clear

    def _admit(self, order):
        approval = self.approval
        if (
            self.live_enabled is not True
            or type(order) is not LiveLimitOrder
            or type(approval) is not LiveOrderApproval
            or approval.account_id != self.account_id
            or approval.order != order
            or not isinstance(approval.expires_at, datetime)
            or approval.expires_at.utcoffset() is None
            or approval.expires_at <= datetime.now(timezone.utc)
            or self.halt_clear is None
            or self.halt_clear() is not True
        ):
            raise OrderRefused("Live order is disabled, stopped, or unapproved")

    def _account(self, *, opening=False):
        account = super()._account(opening=opening)
        if opening and account.get("crypto_status") != "ACTIVE":
            raise OrderRefused("Alpaca live crypto status is inactive or unknown")
        return account


class AlpacaLiveHTTPS:
    """Authenticated transport for the fixed live host. Redirects are refused."""

    def __init__(self, key_id: str, secret_key: str):
        if not all(isinstance(value, str) and value.strip() for value in (key_id, secret_key)):
            raise OrderRefused("Live credentials are required")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def __call__(self, method, url, payload):
        parts = urlsplit(url)
        get_account = method == "GET" and parts.path == "/v2/account" and not parts.query
        get_order = (
            method == "GET" and parts.path == "/v2/orders:by_client_order_id"
            and parts.query.startswith("client_order_id=")
        )
        post_order = method == "POST" and parts.path == "/v2/orders" and not parts.query
        if (
            parts.scheme != "https" or parts.netloc != LIVE_HOST or parts.fragment
            or not (get_account or get_order or post_order)
            or (post_order and not isinstance(payload, dict))
            or (method == "GET" and payload is not None)
        ):
            raise OrderRefused("Unsupported Alpaca live request")

        target = parts.path + (f"?{parts.query}" if parts.query else "")
        body = json.dumps(payload).encode("utf-8") if post_order else None
        try:
            with closing(http.client.HTTPSConnection(LIVE_HOST, timeout=10)) as connection:
                connection.request(method, target, body=body, headers=self._headers)
                response = connection.getresponse()
                if not 200 <= response.status < 300:
                    raise OrderRefused(f"Alpaca live HTTP {response.status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise OrderRefused("Alpaca live response exceeded the size limit")
                return json.loads(raw)
        except OrderRefused:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise OrderRefused("Alpaca live response unavailable") from None
