from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from skippy.adapters.alpaca_live_orders import (
    LIVE_API,
    LIVE_HOST,
    AlpacaLiveHTTPS,
    AlpacaLiveOrders,
    LiveLimitOrder,
    LiveOrderApproval,
)
from skippy.adapters.alpaca_paper_orders import OrderRefused, OrderUncertain, PAPER_API

ORDER = LiveLimitOrder("approved-01", "BTC/USD", "buy", Decimal("0.001"), Decimal("100"))
ACCOUNT = {
    "id": "live-account", "status": "ACTIVE", "crypto_status": "ACTIVE",
    "account_blocked": False, "trading_blocked": False,
}


class Broker:
    def __init__(self):
        self.calls = []
        self.account = ACCOUNT.copy()
        self.reply = None
        self.lose_response = False

    def __call__(self, method, url, payload):
        assert url.startswith(LIVE_API + "/")
        self.calls.append((method, url, payload))
        if url == LIVE_API + "/account":
            return self.account
        if method == "POST":
            self.reply = {**payload, "id": "live-order-id"}
            if self.lose_response:
                raise TimeoutError
        else:
            assert url == f"{LIVE_API}/orders:by_client_order_id?client_order_id={ORDER.client_order_id}"
        return self.reply

    @property
    def posts(self):
        return [payload for method, _, payload in self.calls if method == "POST"]


@pytest.fixture
def setup(tmp_path):
    broker = Broker()
    approval = LiveOrderApproval(
        ACCOUNT["id"], ORDER, datetime.now(timezone.utc) + timedelta(minutes=5)
    )
    settings = dict(account_id=ACCOUNT["id"], transport=broker, live_enabled=True,
                    approval=approval, halt_clear=lambda: True)

    def adapter(**overrides):
        return AlpacaLiveOrders(tmp_path / "orders.sqlite", **(settings | overrides))

    return broker, approval, adapter


@pytest.mark.parametrize("restriction", ["disabled", "missing", "wrong-order", "expired", "halted"])
def test_live_gate_refuses_before_any_request(setup, restriction):
    broker, approval, adapter = setup
    options = {
        "disabled": {"live_enabled": False},
        "missing": {"approval": None},
        "wrong-order": {"approval": replace(approval, order=replace(ORDER, limit_price=Decimal("99")))},
        "expired": {"approval": replace(approval, expires_at=datetime.now(timezone.utc))},
        "halted": {"halt_clear": lambda: False},
    }
    with pytest.raises(OrderRefused):
        adapter(**options[restriction]).submit(ORDER)
    assert broker.calls == []


@pytest.mark.parametrize("field,value", [("id", "paper-account"), ("crypto_status", "INACTIVE")])
def test_wrong_account_or_crypto_status_prevents_post(setup, field, value):
    broker, _, adapter = setup
    broker.account[field] = value
    with pytest.raises(OrderRefused):
        adapter().submit(ORDER)
    assert not broker.posts


def test_default_constructor_never_submits_without_enablement(setup, tmp_path):
    broker, approval, _ = setup
    with pytest.raises(OrderRefused):
        AlpacaLiveOrders(
            tmp_path / "default.sqlite",
            account_id=ACCOUNT["id"],
            transport=broker,
            approval=approval,
            halt_clear=lambda: True,
        ).submit(ORDER)
    assert broker.calls == []


def test_post_is_bounded_and_lost_response_reconciles_without_retry(setup):
    broker, _, adapter = setup
    broker.lose_response = True
    with pytest.raises(OrderUncertain):
        adapter().submit(ORDER)
    with pytest.raises(OrderUncertain):
        adapter().submit(ORDER)
    assert broker.posts == [{
        "client_order_id": "approved-01", "symbol": "BTC/USD", "side": "buy",
        "qty": "0.001", "type": "limit", "time_in_force": "ioc", "limit_price": "100",
    }]
    broker.reply["limit_price"] = "101"
    with pytest.raises(OrderUncertain):
        adapter(live_enabled=False).reconcile(ORDER.client_order_id)
    broker.reply["limit_price"] = "100"
    assert adapter(live_enabled=False).reconcile(ORDER.client_order_id) == "live-order-id"
    assert len(broker.posts) == 1


def test_halt_after_reservation_stops_post(setup):
    broker, _, adapter = setup
    checks = iter((True, False))
    with pytest.raises(OrderUncertain):
        adapter(halt_clear=lambda: next(checks)).submit(ORDER)
    assert not broker.posts


def test_https_transport_pins_host_and_refuses_redirects():
    transport = AlpacaLiveHTTPS("mock-key", "mock-secret")
    with patch("http.client.HTTPSConnection") as connection:
        with pytest.raises(OrderRefused, match="Unsupported"):
            transport("GET", PAPER_API + "/account", None)
        connection.assert_not_called()

        client = connection.return_value
        client.getresponse.return_value.status = 302
        with pytest.raises(OrderRefused, match="HTTP 302"):
            transport("GET", LIVE_API + "/account", None)
        connection.assert_called_once_with(LIVE_HOST, timeout=10)
        assert client.request.call_args.args == ("GET", "/v2/account")
        assert client.request.call_args.kwargs["headers"]["APCA-API-SECRET-KEY"] == "mock-secret"
        client.close.assert_called_once()
