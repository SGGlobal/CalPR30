from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

import pytest

from skippy.adapters.alpaca_paper_orders import (
    PAPER_API,
    AlpacaPaperOrders,
    OrderRefused,
    OrderUncertain,
    PaperMarketOrder,
)

ORDER = PaperMarketOrder("paper-01", "BTC/USD", "buy", Decimal("0.001"))


class PaperBroker:
    def __init__(self):
        self.account = {
            "id": "paper-account", "status": "ACTIVE", "trading_blocked": False,
            "account_blocked": False,
        }
        self.calls = []
        self.reply = None
        self.drop_response = False
        self.account_barrier = None

    def __call__(self, method, url, body):
        assert url.startswith(PAPER_API + "/")
        self.calls.append((method, url, body))
        if url == PAPER_API + "/account":
            assert method == "GET" and body is None
            if self.account_barrier:
                self.account_barrier.wait(timeout=5)
            return self.account
        if method == "POST":
            assert url == PAPER_API + "/orders"
            self.reply = {**body, "id": "broker-order", "symbol": "BTCUSD"}
            if self.drop_response:
                raise TimeoutError("private transport diagnostics")
            return self.reply
        assert url == PAPER_API + "/orders:by_client_order_id?client_order_id=paper-01"
        assert method == "GET" and body is None
        return self.reply

    @property
    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]


@pytest.fixture
def broker():
    return PaperBroker()


def router(tmp_path, broker, allow_submit=lambda _: True):
    return AlpacaPaperOrders(
        tmp_path / "orders.sqlite", account_id="paper-account", transport=broker,
        allow_submit=allow_submit,
    )


def test_disabled_by_default_and_restricted_account_refuse(tmp_path, broker):
    with pytest.raises(OrderRefused):
        router(tmp_path, broker, allow_submit=None).submit(ORDER)
    assert broker.calls == []
    for field, value in (("id", "other-account"), ("trading_blocked", True)):
        original = broker.account[field]
        broker.account[field] = value
        with pytest.raises(OrderRefused):
            router(tmp_path, broker).submit(ORDER)
        broker.account[field] = original
    assert not broker.posts


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("NaN"), Decimal("1e10000"), 0.001])
def test_invalid_quantity_never_reaches_transport(tmp_path, broker, quantity):
    with pytest.raises(OrderRefused):
        router(tmp_path, broker).submit(replace(ORDER, quantity=quantity))
    assert broker.calls == []


def test_submission_is_paper_ioc_and_conflicting_reuse_is_refused(tmp_path, broker):
    assert router(tmp_path, broker).submit(ORDER) == "broker-order"
    assert router(tmp_path, broker).submit(ORDER) == "broker-order"
    with pytest.raises(OrderRefused):
        router(tmp_path, broker).submit(replace(ORDER, side="sell"))
    assert broker.posts == [("POST", PAPER_API + "/orders", {
        "client_order_id": "paper-01", "symbol": "BTC/USD", "side": "buy",
        "qty": "0.001", "type": "market", "time_in_force": "ioc",
    })]


def test_timeout_reconciles_without_resubmitting_even_after_restart(tmp_path, broker):
    broker.drop_response = True
    with pytest.raises(OrderUncertain, match="outcome unverified") as failure:
        router(tmp_path, broker).submit(ORDER)
    assert "private" not in str(failure.value)
    with pytest.raises(OrderUncertain, match="already reserved"):
        router(tmp_path, broker).submit(ORDER)
    broker.reply["symbol"] = "B/TCUSD"
    with pytest.raises(OrderUncertain):
        router(tmp_path, broker).reconcile("paper-01")
    broker.reply["symbol"] = "BTCUSD"
    assert router(tmp_path, broker, allow_submit=None).reconcile("paper-01") == "broker-order"
    assert len(broker.posts) == 1


def test_halt_after_reservation_prevents_post(tmp_path, broker):
    decisions = iter((True, False))
    with pytest.raises(OrderUncertain):
        router(tmp_path, broker, allow_submit=lambda _: next(decisions)).submit(ORDER)
    with pytest.raises(OrderUncertain, match="already reserved"):
        router(tmp_path, broker).submit(ORDER)
    assert not broker.posts


def test_concurrent_callers_send_at_most_one_post(tmp_path, broker):
    first, second = router(tmp_path, broker), router(tmp_path, broker)
    broker.account_barrier = Barrier(2)

    def submit(adapter):
        try:
            return adapter.submit(ORDER)
        except OrderUncertain:
            return "pending"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [first, second]))
    assert "broker-order" in results and len(broker.posts) == 1
