"""QLA Buzzer reasoning dashboard for BTC 5-minute UP/DOWN markets."""

from __future__ import annotations

import time
from dataclasses import dataclass

from polymarket.archive import archive_statistics, save_decision
from polymarket.client import build_client
from polymarket.intelligence import MarketIntelligence, get_market_intelligence
from polymarket.markets import DiscoveredMarket, discover_current_btc_5m_market, format_utc
from polymarket.reasoning import Decision, reason_about_market


REFRESH_SECONDS = 2


@dataclass
class DashboardState:
    question: str = "Loading market..."
    market_slug: str = ""
    market_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    end_timestamp: int = 0
    time_left: int = 0
    up: MarketIntelligence | None = None
    down: MarketIntelligence | None = None
    decision: Decision | None = None
    dry_run: bool = True
    archive_status: str = "Not started"
    decisions_saved: int = 0
    session_count: int = 0
    last_action: str = "Starting"


def _format_metric(value: float | int | None, decimals: int = 4) -> str:
    if value is None:
        return "--"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{decimals}f}"


def _print_intelligence(label: str, token_id: str, metrics: MarketIntelligence | None) -> None:
    print(label)
    print(f"  Token:                {token_id}")
    print(f"  Best Bid:             {_format_metric(metrics.best_bid if metrics else None)}")
    print(f"  Best Ask:             {_format_metric(metrics.best_ask if metrics else None)}")
    print(f"  Mid Price:            {_format_metric(metrics.mid_price if metrics else None)}")
    print(f"  Spread:               {_format_metric(metrics.spread if metrics else None)}")
    print(
        f"  Bid Liquidity:        {_format_metric(metrics.bid_liquidity if metrics else None, 2)}"
    )
    print(
        f"  Ask Liquidity:        {_format_metric(metrics.ask_liquidity if metrics else None, 2)}"
    )
    print(
        f"  Order Book Imbalance: {_format_metric(metrics.order_book_imbalance if metrics else None)}"
    )
    print(
        f"  Largest Bid Wall:     {_format_metric(metrics.largest_bid_wall if metrics else None, 2)}"
    )
    print(
        f"  Largest Ask Wall:     {_format_metric(metrics.largest_ask_wall if metrics else None, 2)}"
    )
    print(
        f"  Time Remaining:       {_format_metric(metrics.time_remaining if metrics else None)} sec"
    )
    print(f"  Bid/Ask Ratio:        {_format_metric(metrics.bid_ask_ratio if metrics else None)}")


def _print_decision(decision: Decision | None) -> None:
    print("Reasoning")
    print(f"  Confidence: {_format_metric(decision.confidence if decision else None)}")
    print(f"  Direction:  {decision.direction if decision else '--'}")
    print(f"  Risk:       {decision.risk if decision else '--'}")
    print("  Explanation:")
    if not decision:
        print("    --")
        return
    for reason in decision.explanation:
        print(f"    - {reason}")


def _print_archive_status(state: DashboardState) -> None:
    print("Archive Status")
    print(f"  Status:                {state.archive_status}")
    print(f"  Decisions Saved:       {state.decisions_saved}")
    print(f"  Current Session Count: {state.session_count}")


def render_dashboard(state: DashboardState) -> None:
    print("\033[2J\033[H", end="")
    print("QLA Phase 6 - Decision Archive")
    print("=" * 60)
    print("Question:")
    print(state.question)
    print()
    print("Market Slug:")
    print(state.market_slug)
    print()
    print("Market ID:")
    print(state.market_id)
    print()
    print("End Time:")
    print(format_utc(state.end_timestamp) if state.end_timestamp else "")
    print()
    print("Time Remaining:")
    print(f"{state.time_left} sec")
    print()
    _print_intelligence("UP", state.up_token_id, state.up)
    print()
    _print_intelligence("DOWN", state.down_token_id, state.down)
    print()
    _print_decision(state.decision)
    print()
    _print_archive_status(state)
    print()
    print(f"DRY_RUN: {state.dry_run}")
    print(f"Status: {state.last_action}")


def dashboard_state_from_market(
    market: DiscoveredMarket,
    dry_run: bool,
    session_count: int,
    now: int | None = None,
) -> DashboardState:
    time_left = market.seconds_remaining(now)
    up = get_market_intelligence(market.up_token_id, time_left)
    down = get_market_intelligence(market.down_token_id, time_left)
    decision = reason_about_market(up, down)
    timestamp = now if now is not None else int(time.time())
    stats_before = archive_statistics()
    decisions_saved = int(stats_before["decision_count"])
    archive_status = "Ready"

    try:
        save_decision(
            timestamp=timestamp,
            market_id=market.market_id,
            market_slug=market.slug,
            market_question=market.question,
            seconds_remaining=time_left,
            intelligence={"up": up, "down": down},
            decision=decision,
            dry_run=dry_run,
        )
        decisions_saved += 1
        session_count += 1
        archive_status = "Saved"
    except OSError as exc:
        archive_status = f"Archive error: {exc}"

    return DashboardState(
        question=market.question,
        market_slug=market.slug,
        market_id=market.market_id,
        up_token_id=market.up_token_id,
        down_token_id=market.down_token_id,
        end_timestamp=market.end_timestamp,
        time_left=time_left,
        up=up,
        down=down,
        decision=decision,
        dry_run=dry_run,
        archive_status=archive_status,
        decisions_saved=decisions_saved,
        session_count=session_count,
        last_action="Reasoning refreshed and archived; no orders, trade execution, or profitability evaluation enabled",
    )


def run_qla_buzzer() -> None:
    """Run the autonomous reasoning dashboard without placing orders."""
    client = build_client()
    session_count = 0
    state = DashboardState(dry_run=client.settings.dry_run)

    while True:
        try:
            now = int(time.time())
            market = discover_current_btc_5m_market(client, now)
            state = dashboard_state_from_market(market, client.settings.dry_run, session_count, now)
            session_count = state.session_count
            render_dashboard(state)
        except KeyboardInterrupt:
            print("\nQLA stopped by user.")
            return
        except Exception as exc:
            state.last_action = f"Reasoning dashboard error: {exc}"
            render_dashboard(state)

        time.sleep(REFRESH_SECONDS)
