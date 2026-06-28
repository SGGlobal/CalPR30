"""Reusable market intelligence metrics for Polymarket order books."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .books import get_order_book


@dataclass(frozen=True)
class MarketIntelligence:
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    spread: float | None
    bid_liquidity: float
    ask_liquidity: float
    order_book_imbalance: float | None
    largest_bid_wall: float | None
    largest_ask_wall: float | None
    time_remaining: int
    bid_ask_ratio: float | None


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_book_sides(book: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    bids = book.get("bids") or book.get("buy") or []
    asks = book.get("asks") or book.get("sell") or []
    return bids, asks


def level_price(level: Any) -> float | None:
    if isinstance(level, dict):
        return to_float(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return to_float(level[0])
    return None


def level_size(level: Any) -> float | None:
    if isinstance(level, dict):
        return to_float(level.get("size") or level.get("quantity"))
    if isinstance(level, (list, tuple)) and len(level) > 1:
        return to_float(level[1])
    return None


def best_bid_from_book(book: dict[str, Any]) -> float | None:
    bids, _ = get_book_sides(book)
    prices = [price for price in (level_price(level) for level in bids) if price is not None]
    return max(prices) if prices else None


def best_ask_from_book(book: dict[str, Any]) -> float | None:
    _, asks = get_book_sides(book)
    prices = [price for price in (level_price(level) for level in asks) if price is not None]
    return min(prices) if prices else None


def calculate_mid_price(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2


def calculate_spread_from_prices(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return best_ask - best_bid


def calculate_liquidity(levels: list[Any]) -> float:
    sizes = [size for size in (level_size(level) for level in levels) if size is not None]
    return sum(sizes)


def calculate_order_book_imbalance(bid_liquidity: float, ask_liquidity: float) -> float | None:
    total = bid_liquidity + ask_liquidity
    if total == 0:
        return None
    return (bid_liquidity - ask_liquidity) / total


def largest_wall(levels: list[Any]) -> float | None:
    sizes = [size for size in (level_size(level) for level in levels) if size is not None]
    return max(sizes) if sizes else None


def calculate_bid_ask_ratio(bid_liquidity: float, ask_liquidity: float) -> float | None:
    if ask_liquidity == 0:
        return None
    return bid_liquidity / ask_liquidity


def compute_market_intelligence(book: dict[str, Any], time_remaining: int) -> MarketIntelligence:
    bids, asks = get_book_sides(book)
    best_bid = best_bid_from_book(book)
    best_ask = best_ask_from_book(book)
    bid_liquidity = calculate_liquidity(bids)
    ask_liquidity = calculate_liquidity(asks)

    return MarketIntelligence(
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=calculate_mid_price(best_bid, best_ask),
        spread=calculate_spread_from_prices(best_bid, best_ask),
        bid_liquidity=bid_liquidity,
        ask_liquidity=ask_liquidity,
        order_book_imbalance=calculate_order_book_imbalance(bid_liquidity, ask_liquidity),
        largest_bid_wall=largest_wall(bids),
        largest_ask_wall=largest_wall(asks),
        time_remaining=max(0, int(time_remaining)),
        bid_ask_ratio=calculate_bid_ask_ratio(bid_liquidity, ask_liquidity),
    )


def get_market_intelligence(token_id: str, time_remaining: int) -> MarketIntelligence:
    """Fetch a cached order book and compute reusable intelligence metrics."""
    return compute_market_intelligence(get_order_book(token_id), time_remaining)
