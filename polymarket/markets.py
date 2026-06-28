"""Market discovery helpers."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .client import build_client


FIVE_MINUTES = 300


def get_current_5m_timestamp() -> int:
    """Return the current UTC epoch rounded down to a 5-minute bucket."""
    now = int(time.time())
    return now - (now % FIVE_MINUTES)


def get_time_remaining(market_ts: int) -> int:
    """Return seconds remaining in a 5-minute market bucket."""
    close_ts = int(market_ts) + FIVE_MINUTES
    return max(0, close_ts - int(time.time()))


def get_market_by_slug(slug: str) -> dict[str, Any] | None:
    """Fetch a Polymarket market by slug from Gamma."""
    client = build_client()
    data = client.get_json(f"{client.gamma_base_url}/markets", {"slug": slug})
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        markets = data.get("markets") or data.get("data") or []
        return markets[0] if markets else data
    return None


def extract_tokens(market: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Extract UP and DOWN token metadata from a Gamma market response."""
    candidates = market.get("tokens") or market.get("outcomes") or market.get("clobTokenIds") or []
    up_token: dict[str, Any] | None = None
    down_token: dict[str, Any] | None = None

    if isinstance(candidates, str):
        import json

        try:
            candidates = json.loads(candidates)
        except json.JSONDecodeError:
            candidates = []

    for index, token in enumerate(candidates):
        if isinstance(token, str):
            label = "UP" if index == 0 else "DOWN"
            token_data = {"token_id": token, "outcome": label}
        else:
            outcome = str(token.get("outcome") or token.get("name") or token.get("label") or "").upper()
            token_id = token.get("token_id") or token.get("tokenId") or token.get("id")
            token_data = {"token_id": token_id, "outcome": outcome, **token}
            label = outcome

        if "UP" in label:
            up_token = token_data
        elif "DOWN" in label:
            down_token = token_data

    return up_token, down_token


def format_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
