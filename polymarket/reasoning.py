"""Reusable reasoning engine for QLA market intelligence."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from polymarket.intelligence import MarketIntelligence


Direction = str
RiskLevel = str


@dataclass(frozen=True)
class Decision:
    confidence: int
    direction: Direction
    risk: RiskLevel
    explanation: list[str]
    metrics_used: dict[str, float | int | None]
    timestamp: int


class ReasoningStrategy(Protocol):
    def evaluate(self, up: MarketIntelligence, down: MarketIntelligence, timestamp: int | None = None) -> Decision:
        ...


@dataclass(frozen=True)
class OrderBookReasoningStrategy:
    imbalance_threshold: float = 0.08
    ratio_threshold: float = 1.15
    wide_spread_threshold: float = 0.05
    low_time_threshold: int = 30
    max_confidence: int = 85
    metrics: tuple[str, ...] = field(
        default=(
            "mid_price",
            "spread",
            "bid_liquidity",
            "ask_liquidity",
            "order_book_imbalance",
            "largest_bid_wall",
            "largest_ask_wall",
            "time_remaining",
            "bid_ask_ratio",
        )
    )

    def evaluate(self, up: MarketIntelligence, down: MarketIntelligence, timestamp: int | None = None) -> Decision:
        return reason_about_market(up, down, self, timestamp)


def clamp_confidence(value: float) -> int:
    return max(0, min(100, round(value)))


def choose_direction(up: MarketIntelligence, down: MarketIntelligence, strategy: OrderBookReasoningStrategy) -> tuple[Direction, list[str]]:
    explanation: list[str] = []
    up_score = _score_side(up, strategy)
    down_score = _score_side(down, strategy)
    score_delta = up_score - down_score

    if abs(score_delta) < strategy.imbalance_threshold:
        explanation.append("UP and DOWN order-book pressure are close, so direction is neutral.")
        return "NEUTRAL", explanation

    if score_delta > 0:
        explanation.append("UP shows stronger relative order-book support than DOWN.")
        return "UP", explanation

    explanation.append("DOWN shows stronger relative order-book support than UP.")
    return "DOWN", explanation


def assess_risk(up: MarketIntelligence, down: MarketIntelligence, strategy: OrderBookReasoningStrategy) -> tuple[RiskLevel, list[str]]:
    explanation: list[str] = []
    widest_spread = max(_or_zero(up.spread), _or_zero(down.spread))
    time_remaining = min(up.time_remaining, down.time_remaining)

    if time_remaining <= strategy.low_time_threshold:
        explanation.append("Time remaining is low, increasing uncertainty.")
        return "HIGH", explanation

    if widest_spread >= strategy.wide_spread_threshold:
        explanation.append("At least one side has a wide spread, increasing execution uncertainty.")
        return "HIGH", explanation

    if widest_spread >= strategy.wide_spread_threshold / 2:
        explanation.append("Spreads are moderate, so risk is medium.")
        return "MEDIUM", explanation

    explanation.append("Spreads are tight and time remaining is sufficient, so risk is low.")
    return "LOW", explanation


def calculate_confidence(
    direction: Direction,
    risk: RiskLevel,
    up: MarketIntelligence,
    down: MarketIntelligence,
    strategy: OrderBookReasoningStrategy,
) -> int:
    if direction == "NEUTRAL":
        return 50

    score_delta = abs(_score_side(up, strategy) - _score_side(down, strategy))
    confidence = 50 + min(strategy.max_confidence - 50, score_delta * 100)
    if risk == "MEDIUM":
        confidence -= 10
    elif risk == "HIGH":
        confidence -= 20
    return clamp_confidence(confidence)


def metrics_used(up: MarketIntelligence, down: MarketIntelligence) -> dict[str, float | int | None]:
    return {
        "up_mid_price": up.mid_price,
        "up_spread": up.spread,
        "up_bid_liquidity": up.bid_liquidity,
        "up_ask_liquidity": up.ask_liquidity,
        "up_order_book_imbalance": up.order_book_imbalance,
        "up_largest_bid_wall": up.largest_bid_wall,
        "up_largest_ask_wall": up.largest_ask_wall,
        "up_time_remaining": up.time_remaining,
        "up_bid_ask_ratio": up.bid_ask_ratio,
        "down_mid_price": down.mid_price,
        "down_spread": down.spread,
        "down_bid_liquidity": down.bid_liquidity,
        "down_ask_liquidity": down.ask_liquidity,
        "down_order_book_imbalance": down.order_book_imbalance,
        "down_largest_bid_wall": down.largest_bid_wall,
        "down_largest_ask_wall": down.largest_ask_wall,
        "down_time_remaining": down.time_remaining,
        "down_bid_ask_ratio": down.bid_ask_ratio,
    }


def reason_about_market(
    up: MarketIntelligence,
    down: MarketIntelligence,
    strategy: OrderBookReasoningStrategy | None = None,
    timestamp: int | None = None,
) -> Decision:
    active_strategy = strategy or OrderBookReasoningStrategy()
    decision_timestamp = int(time.time()) if timestamp is None else int(timestamp)
    direction, direction_reasons = choose_direction(up, down, active_strategy)
    risk, risk_reasons = assess_risk(up, down, active_strategy)
    confidence = calculate_confidence(direction, risk, up, down, active_strategy)

    explanation = [*direction_reasons, *risk_reasons]
    explanation.append("Decision is informational only; no orders or profitability evaluation are performed.")

    return Decision(
        confidence=confidence,
        direction=direction,
        risk=risk,
        explanation=explanation,
        metrics_used=metrics_used(up, down),
        timestamp=decision_timestamp,
    )


def _score_side(metrics: MarketIntelligence, strategy: OrderBookReasoningStrategy) -> float:
    score = _or_zero(metrics.order_book_imbalance)
    ratio = metrics.bid_ask_ratio
    if ratio is not None:
        if ratio >= strategy.ratio_threshold:
            score += 0.05
        elif ratio <= 1 / strategy.ratio_threshold:
            score -= 0.05
    return score


def _or_zero(value: float | None) -> float:
    return 0.0 if value is None else value
