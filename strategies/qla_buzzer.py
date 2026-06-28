"""QLA Buzzer strategy for BTC 5-minute UP/DOWN Polymarket markets."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from polymarket.books import get_best_bid_ask
from polymarket.client import build_client
from polymarket.logger import log_event
from polymarket.markets import extract_tokens, get_current_5m_timestamp, get_market_by_slug, get_time_remaining
from polymarket.orders import get_order_status, place_limit_order
from polymarket.risk import calculate_shares


FAVORITE_THRESHOLD = float(os.getenv("FAVORITE_THRESHOLD", "0.70"))
MAX_ASK_CAP = float(os.getenv("MAX_ASK_CAP", "0.97"))
REFRESH_SECONDS = 2
ENTRY_WINDOW_SECONDS = 90
STOP_NEW_ORDERS_SECONDS = 5


@dataclass
class BookState:
    bid: float | None = None
    ask: float | None = None


@dataclass
class DashboardState:
    question: str = "Loading market..."
    time_left: int = 0
    up: BookState = field(default_factory=BookState)
    down: BookState = field(default_factory=BookState)
    favored_side: str = "None"
    last_action: str = "Starting"
    attempts: int = 0
    entries: int = 0
    skips: int = 0
    dry_run: bool = True


def _price(value: float | None) -> str:
    return "--" if value is None else f"{value:.3f}"


def render_dashboard(state: DashboardState) -> None:
    print("\033[2J\033[H", end="")
    print("QLA")
    print("=" * 60)
    print(f"Current market question: {state.question}")
    print(f"Time left: {state.time_left}s")
    print(f"UP bid/ask: {_price(state.up.bid)} / {_price(state.up.ask)}")
    print(f"DOWN bid/ask: {_price(state.down.bid)} / {_price(state.down.ask)}")
    print(f"Favored side: {state.favored_side}")
    print(f"Last action: {state.last_action}")
    print(f"Attempts: {state.attempts}")
    print(f"Entries: {state.entries}")
    print(f"Skips: {state.skips}")
    print(f"Dry run: {state.dry_run}")


def _pick_favored(up_ask: float | None, down_ask: float | None) -> tuple[str | None, float | None]:
    candidates: list[tuple[str, float]] = []
    if up_ask is not None and FAVORITE_THRESHOLD <= up_ask <= MAX_ASK_CAP:
        candidates.append(("UP", up_ask))
    if down_ask is not None and FAVORITE_THRESHOLD <= down_ask <= MAX_ASK_CAP:
        candidates.append(("DOWN", down_ask))
    if not candidates:
        return None, None
    return max(candidates, key=lambda item: item[1])


def _order_id(response: dict[str, Any]) -> str | None:
    value = response.get("id") or response.get("order_id") or response.get("orderId")
    return str(value) if value else None


def _is_order_safe_to_retry(order_id: str) -> bool:
    try:
        status = get_order_status(order_id)
    except Exception as exc:
        log_event("order_status_failed", order_id=order_id, error=str(exc))
        return False
    known_done = {"cancelled", "canceled", "filled", "expired", "dry_run"}
    return str(status.get("status", "")).lower() in known_done


def run_qla_buzzer() -> None:
    """Run the QLA Buzzer loop without ever bypassing DRY_RUN safeguards."""
    client = build_client()
    state = DashboardState(dry_run=client.settings.dry_run)
    last_market_ts: int | None = None
    last_order_id: str | None = None
    entered_market_ts: int | None = None

    while True:
        market_ts = get_current_5m_timestamp()
        if market_ts != last_market_ts:
            last_market_ts = market_ts
            last_order_id = None
            entered_market_ts = None
            state.last_action = "New 5m market window"

        slug = f"btc-updown-5m-{market_ts}"
        time_left = get_time_remaining(market_ts)
        state.time_left = time_left

        try:
            market = get_market_by_slug(slug)
            if not market:
                state.question = slug
                state.favored_side = "None"
                state.last_action = "Market not found"
                state.skips += 1
                render_dashboard(state)
                time.sleep(REFRESH_SECONDS)
                continue

            state.question = str(market.get("question") or market.get("title") or slug)
            up_token, down_token = extract_tokens(market)
            if not up_token or not down_token:
                state.last_action = "Could not extract UP/DOWN tokens"
                state.skips += 1
                render_dashboard(state)
                time.sleep(REFRESH_SECONDS)
                continue

            up_token_id = str(up_token["token_id"])
            down_token_id = str(down_token["token_id"])
            up_bid, up_ask = get_best_bid_ask(up_token_id)
            down_bid, down_ask = get_best_bid_ask(down_token_id)
            state.up = BookState(up_bid, up_ask)
            state.down = BookState(down_bid, down_ask)

            favored_side, favored_ask = _pick_favored(up_ask, down_ask)
            state.favored_side = favored_side or "None"

            if time_left > ENTRY_WINDOW_SECONDS:
                state.last_action = "Waiting for final 90 seconds"
                state.skips += 1
            elif time_left <= STOP_NEW_ORDERS_SECONDS:
                state.last_action = "Stopped new orders with <=5s left"
                state.skips += 1
            elif entered_market_ts == market_ts:
                state.last_action = "Already entered this market"
                state.skips += 1
            elif last_order_id and not _is_order_safe_to_retry(last_order_id):
                state.last_action = "Existing order uncertain; refusing duplicate"
                state.skips += 1
            elif not favored_side or favored_ask is None:
                state.last_action = "No qualifying favored side"
                state.skips += 1
            else:
                token_id = up_token_id if favored_side == "UP" else down_token_id
                size = calculate_shares(client.settings.bet_size_usd, favored_ask)
                state.attempts += 1
                try:
                    response = place_limit_order(token_id, "BUY", favored_ask, size)
                    last_order_id = _order_id(response) or "unknown-after-submit"
                    entered_market_ts = market_ts
                    state.entries += 1
                    state.last_action = f"Placed {favored_side} limit order at {favored_ask:.3f}"
                    log_event("entry", market_slug=slug, side=favored_side, price=favored_ask, size=size, order_id=last_order_id)
                except Exception as exc:
                    last_order_id = "unknown-after-submit"
                    state.last_action = "Order attempt failed; refusing duplicates"
                    log_event("order_attempt_failed", market_slug=slug, side=favored_side, error=str(exc))

            render_dashboard(state)
        except KeyboardInterrupt:
            print("\nQLA stopped by user.")
            return
        except Exception as exc:
            state.last_action = f"API/runtime error: {exc}"
            state.skips += 1
            log_event("runtime_error", error=str(exc))
            render_dashboard(state)

        time.sleep(REFRESH_SECONDS)
