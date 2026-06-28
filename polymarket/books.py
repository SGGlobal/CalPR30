"""Order book helpers."""

from __future__ import annotations

import time
import urllib.error
from typing import Any

from .client import PolymarketClient, build_client
from .logger import log_event


ORDER_BOOK_CACHE_SECONDS = 2
_ORDER_BOOK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


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


def _book_sides(book: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    bids = book.get("bids") or book.get("buy") or []
    asks = book.get("asks") or book.get("sell") or []
    return bids, asks


def clear_order_book_cache() -> None:
    """Clear cached order books for tests or manual refreshes."""
    _ORDER_BOOK_CACHE.clear()


def get_order_book(
    token_id: str,
    client: PolymarketClient | None = None,
    now: float | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch and cache a live order book for a token id."""
    current_time = time.time() if now is None else now
    cached = _ORDER_BOOK_CACHE.get(token_id)
    if not force_refresh and cached and current_time - cached[0] < ORDER_BOOK_CACHE_SECONDS:
        return cached[1]

    api_client = client or build_client()
    try:
        book = api_client.get_json(f"{api_client.clob_base_url}/book", {"token_id": token_id})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        log_event("order_book_api_failure", token_id=token_id, error=str(error))
        raise

    if not isinstance(book, dict):
        log_event("order_book_api_failure", token_id=token_id, error="Unexpected order book response")
        raise ValueError("Unexpected order book response")

    _ORDER_BOOK_CACHE[token_id] = (current_time, book)
    return book


def get_best_bid(token_id: str) -> float | None:
    """Return the best bid for a token id."""
    bids, _ = _book_sides(get_order_book(token_id))
    return _best_price(bids, highest=True)


def get_best_ask(token_id: str) -> float | None:
    """Return the best ask for a token id."""
    _, asks = _book_sides(get_order_book(token_id))
    return _best_price(asks, highest=False)


def calculate_spread(token_id: str) -> float | None:
    """Return best ask minus best bid for a token id."""
    bid = get_best_bid(token_id)
    ask = get_best_ask(token_id)
    if bid is None or ask is None:
        return None
    return ask - bid


def get_best_bid_ask(token_id: str) -> tuple[float | None, float | None]:
    """Return the best bid and ask for a token id."""
    bids, asks = _book_sides(get_order_book(token_id))
    return _best_price(bids, highest=True), _best_price(asks, highest=False)
