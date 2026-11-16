"""QLA dry-run order path. Refusal paths must not open a socket."""

from __future__ import annotations

import pytest

import main_qla
from polymarket.client import PolymarketClient, Settings, get_settings
from polymarket.orders import (
    _LIVE_ORDER_REFUSAL,
    cancel_token_orders,
    get_order_status,
    place_limit_order,
)


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Ignore a developer .env and any inherited DRY_RUN."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DRY_RUN", raising=False)
    return monkeypatch


@pytest.fixture
def http_calls(monkeypatch):
    calls = {"post_json": [], "delete_json": [], "get_json": []}

    def post_json(self, url, payload):
        calls["post_json"].append((url, payload))
        return {"called": "post_json"}

    def delete_json(self, url, payload):
        calls["delete_json"].append((url, payload))
        return {"called": "delete_json"}

    def get_json(self, url, params=None):
        calls["get_json"].append((url, params))
        return {"called": "get_json"}

    monkeypatch.setattr(PolymarketClient, "post_json", post_json)
    monkeypatch.setattr(PolymarketClient, "delete_json", delete_json)
    monkeypatch.setattr(PolymarketClient, "get_json", get_json)
    monkeypatch.setattr("polymarket.orders.log_event", lambda *args, **kwargs: None)
    return calls


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        ("true", True),
        ("false", False),
        ("nope", True),
    ],
)
def test_get_settings_reads_dry_run(isolated_env, raw, expected):
    if raw is None:
        isolated_env.delenv("DRY_RUN", raising=False)
    else:
        isolated_env.setenv("DRY_RUN", raw)

    assert get_settings().dry_run is expected


def test_live_placement_and_cancel_do_not_call_http(isolated_env, http_calls):
    isolated_env.setenv("DRY_RUN", "false")

    with pytest.raises(NotImplementedError, match=_LIVE_ORDER_REFUSAL):
        place_limit_order("token", "buy", 0.5, 1.0)
    with pytest.raises(NotImplementedError, match=_LIVE_ORDER_REFUSAL):
        cancel_token_orders("token")

    assert http_calls == {"post_json": [], "delete_json": [], "get_json": []}


def test_dry_run_status_lookup_stays_local(http_calls):
    assert get_order_status("dry-run-abc") == {
        "id": "dry-run-abc",
        "status": "dry_run",
    }
    assert http_calls["get_json"] == []


def test_other_status_lookup_refuses_without_http(http_calls):
    with pytest.raises(NotImplementedError, match=_LIVE_ORDER_REFUSAL):
        get_order_status("live-order-1")

    assert http_calls["get_json"] == []
    assert http_calls["post_json"] == []
    assert http_calls["delete_json"] == []


def test_require_configuration_rejects_live_mode():
    settings = Settings(
        private_key="k",
        public_key="p",
        poly_api_key="a",
        poly_api_secret="s",
        poly_passphrase="pp",
        dry_run=False,
        bet_size_usd=5.0,
        environment_loaded=True,
    )
    client = PolymarketClient(settings)

    with pytest.raises(main_qla.ConnectionCheckError, match="DRY_RUN must remain True"):
        main_qla._require_configuration(client)
