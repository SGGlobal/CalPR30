"""Order book helpers."""

from __future__ import annotations

from typing import Any

from .client import build_client


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _best_price(levels: list[Any], highest: bool) -> float | None:
    prices: list[float] = []
    for level in levels or []:
        if isinstance(level, dict):
            price = _to_float(level.get("price"))
        elif isinstance(level, (list, tuple)) and level:
            price = _to_float(level[0])
        else:
            price = None
        if price is not None:
            prices.append(price)
    if not prices:
        return None
    return max(prices) if highest else min(prices)


def get_order_book(token_id: str) -> dict[str, Any]:
    """Fetch an order book for a token id."""
    client = build_client()
    return client.get_json(f"{client.clob_base_url}/book", {"token_id": token_id})


def get_best_bid_ask(token_id: str) -> tuple[float | None, float | None]:
    """Return the best bid and ask for a token id."""
    book = get_order_book(token_id)
    bids = book.get("bids") or book.get("buy") or []
    asks = book.get("asks") or book.get("sell") or []
    return _best_price(bids, highest=True), _best_price(asks, highest=False)
