"""Market discovery helpers."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .client import PolymarketClient, build_client


FIVE_MINUTES = 300
BTC_5M_SLUG_PREFIX = "btc-updown-5m-"
_BTC_5M_SLUG_RE = re.compile(r"btc-updown-5m-(\d+)$")
_DISCOVERY_CACHE: "DiscoveredMarket | None" = None


@dataclass(frozen=True)
class DiscoveredMarket:
    question: str
    slug: str
    market_id: str
    up_token_id: str
    down_token_id: str
    end_timestamp: int

    def seconds_remaining(self, now: int | None = None) -> int:
        current_time = int(time.time()) if now is None else int(now)
        return max(0, self.end_timestamp - current_time)


class MarketDiscoveryError(Exception):
    """Raised when the current BTC 5-minute market cannot be discovered."""


def get_current_5m_timestamp(now: int | None = None) -> int:
    """Return the current UTC epoch rounded down to a 5-minute bucket."""
    current_time = int(time.time()) if now is None else int(now)
    return current_time - (current_time % FIVE_MINUTES)


def get_time_remaining(market_ts: int, now: int | None = None) -> int:
    """Return seconds remaining in a 5-minute market bucket."""
    current_time = int(time.time()) if now is None else int(now)
    close_ts = int(market_ts) + FIVE_MINUTES
    return max(0, close_ts - current_time)


def get_market_by_slug(slug: str, client: PolymarketClient | None = None) -> dict[str, Any] | None:
    """Fetch a Polymarket market by slug from Gamma."""
    api_client = client or build_client()
    data = api_client.get_json(f"{api_client.gamma_base_url}/markets", {"slug": slug})
    markets = _market_list(data)
    return markets[0] if markets else None


def extract_tokens(market: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Extract UP and DOWN token metadata from a Gamma market response."""
    token_ids = _decode_json_field(market.get("clobTokenIds"))
    outcomes = _decode_json_field(market.get("outcomes"))
    candidates = market.get("tokens") or []

    if token_ids:
        candidates = [
            {"token_id": token_id, "outcome": outcomes[index] if index < len(outcomes) else ("UP" if index == 0 else "DOWN")}
            for index, token_id in enumerate(token_ids)
        ]
    else:
        candidates = _decode_json_field(candidates)

    up_token: dict[str, Any] | None = None
    down_token: dict[str, Any] | None = None

    for index, token in enumerate(candidates):
        if isinstance(token, str):
            label = "UP" if index == 0 else "DOWN"
            token_data = {"token_id": token, "outcome": label}
        elif isinstance(token, dict):
            outcome = str(token.get("outcome") or token.get("name") or token.get("label") or "").upper()
            token_id = token.get("token_id") or token.get("tokenId") or token.get("id")
            token_data = {"token_id": token_id, "outcome": outcome, **token}
            label = outcome or ("UP" if index == 0 else "DOWN")
        else:
            continue

        if "UP" in label:
            up_token = token_data
        elif "DOWN" in label:
            down_token = token_data

    return up_token, down_token


def discover_current_btc_5m_market(
    client: PolymarketClient | None = None,
    now: int | None = None,
    force_refresh: bool = False,
) -> DiscoveredMarket:
    """Discover and cache the current active BTC 5-minute UP/DOWN market."""
    global _DISCOVERY_CACHE

    current_time = int(time.time()) if now is None else int(now)
    if not force_refresh and _DISCOVERY_CACHE and _DISCOVERY_CACHE.seconds_remaining(current_time) > 0:
        return _DISCOVERY_CACHE

    api_client = client or build_client()
    market_ts = get_current_5m_timestamp(current_time)
    slug = f"{BTC_5M_SLUG_PREFIX}{market_ts}"
    market = get_market_by_slug(slug, api_client)

    if not _is_active_btc_5m_market(market, current_time):
        market = _find_active_btc_5m_market(api_client, current_time)

    if not market:
        raise MarketDiscoveryError("No active BTC 5-minute market found")

    discovered = build_discovered_market(market, current_time)
    _DISCOVERY_CACHE = discovered
    return discovered


def build_discovered_market(market: dict[str, Any], now: int | None = None) -> DiscoveredMarket:
    """Build normalized discovered-market metadata from a Gamma market payload."""
    slug = str(market.get("slug") or "")
    question = str(market.get("question") or market.get("title") or slug)
    market_id = str(market.get("id") or market.get("conditionId") or market.get("marketMakerAddress") or "")
    end_timestamp = _market_end_timestamp(market)
    if end_timestamp is None:
        raise MarketDiscoveryError(f"Could not determine end timestamp for market {slug}")

    up_token, down_token = extract_tokens(market)
    if not up_token or not down_token or not up_token.get("token_id") or not down_token.get("token_id"):
        raise MarketDiscoveryError(f"Could not extract UP/DOWN tokens for market {slug}")

    return DiscoveredMarket(
        question=question,
        slug=slug,
        market_id=market_id,
        up_token_id=str(up_token["token_id"]),
        down_token_id=str(down_token["token_id"]),
        end_timestamp=end_timestamp,
    )


def format_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _find_active_btc_5m_market(client: PolymarketClient, now: int) -> dict[str, Any] | None:
    data = client.get_json(
        f"{client.gamma_base_url}/markets",
        {"active": "true", "closed": "false", "limit": 200, "order": "endDate", "ascending": "true"},
    )
    candidates = [market for market in _market_list(data) if _is_active_btc_5m_market(market, now)]
    if not candidates:
        return None
    return min(candidates, key=lambda market: _market_end_timestamp(market) or now + FIVE_MINUTES)


def _is_active_btc_5m_market(market: dict[str, Any] | None, now: int) -> bool:
    if not market:
        return False
    slug = str(market.get("slug") or "")
    question = str(market.get("question") or market.get("title") or "").lower()
    if not slug.startswith(BTC_5M_SLUG_PREFIX) and not ("btc" in question and "5" in question):
        return False
    end_timestamp = _market_end_timestamp(market)
    return end_timestamp is not None and now < end_timestamp


def _market_end_timestamp(market: dict[str, Any]) -> int | None:
    for key in ("endDate", "end_date", "endDateIso", "expirationTime", "endTimestamp"):
        value = market.get(key)
        timestamp = _coerce_timestamp(value)
        if timestamp is not None:
            return timestamp

    slug = str(market.get("slug") or "")
    match = _BTC_5M_SLUG_RE.search(slug)
    if match:
        return int(match.group(1)) + FIVE_MINUTES
    return None


def _coerce_timestamp(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = int(value)
        return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return _coerce_timestamp(int(stripped))
        try:
            normalized = stripped.replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp())
        except ValueError:
            return None
    return None


def _decode_json_field(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _market_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("markets", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    return []
