"""JSON Lines decision archive for QLA market analysis."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .intelligence import MarketIntelligence
from .reasoning import Decision

ARCHIVE_DIR = Path("archive")
LOGS_DIR = Path("logs")
DEFAULT_ARCHIVE_PATH = ARCHIVE_DIR / "decisions.jsonl"


SENSITIVE_KEYS = {
    "private_key",
    "privatekey",
    "api_secret",
    "apisecret",
    "secret",
    "wallet_secret",
    "walletsecret",
    "key",
}


def ensure_archive_directories() -> None:
    """Create local archive directories used by the read-only decision archive."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def intelligence_metrics(metrics: MarketIntelligence) -> dict[str, float | int | None]:
    return {
        "best_bid": metrics.best_bid,
        "best_ask": metrics.best_ask,
        "mid_price": metrics.mid_price,
        "spread": metrics.spread,
        "bid_liquidity": metrics.bid_liquidity,
        "ask_liquidity": metrics.ask_liquidity,
        "order_book_imbalance": metrics.order_book_imbalance,
        "largest_bid_wall": metrics.largest_bid_wall,
        "largest_ask_wall": metrics.largest_ask_wall,
        "time_remaining": metrics.time_remaining,
        "bid_ask_ratio": metrics.bid_ask_ratio,
    }


def decision_metrics(decision: Decision) -> dict[str, Any]:
    return {
        "confidence": decision.confidence,
        "direction": decision.direction,
        "risk": decision.risk,
        "explanation": list(decision.explanation),
        "metrics_used": dict(decision.metrics_used),
        "timestamp": decision.timestamp,
    }


def scrub_sensitive_values(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, nested_value in value.items():
            normalized_key = str(key).lower().replace("_", "")
            if normalized_key in SENSITIVE_KEYS:
                continue
            clean[str(key)] = scrub_sensitive_values(nested_value)
        return clean
    if isinstance(value, list):
        return [scrub_sensitive_values(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_sensitive_values(item) for item in value]
    return value


def save_decision(
    *,
    timestamp: int,
    market_id: str,
    market_slug: str,
    market_question: str,
    seconds_remaining: int,
    intelligence: dict[str, MarketIntelligence],
    decision: Decision,
    dry_run: bool,
    archive_path: Path = DEFAULT_ARCHIVE_PATH,
) -> dict[str, Any]:
    """Append a market analysis decision to the JSON Lines archive."""
    ensure_archive_directories()
    record = {
        "timestamp": int(timestamp),
        "market_id": market_id,
        "market_slug": market_slug,
        "market_question": market_question,
        "seconds_remaining": int(seconds_remaining),
        "intelligence_metrics": {
            label: intelligence_metrics(metrics) for label, metrics in intelligence.items()
        },
        "reasoning_decision": decision_metrics(decision),
        "dry_run": bool(dry_run),
    }
    safe_record = scrub_sensitive_values(record)

    with archive_path.open("a", encoding="utf-8") as archive_file:
        archive_file.write(json.dumps(safe_record, sort_keys=True) + "\n")

    return safe_record


def load_decisions(
    archive_path: Path = DEFAULT_ARCHIVE_PATH, limit: int | None = None
) -> list[dict[str, Any]]:
    """Load archived decision records from JSON Lines."""
    ensure_archive_directories()
    if not archive_path.exists():
        return []

    records: list[dict[str, Any]] = []
    with archive_path.open("r", encoding="utf-8") as archive_file:
        for line in archive_file:
            stripped = line.strip()
            if not stripped:
                continue
            records.append(json.loads(stripped))
            if limit is not None and len(records) >= limit:
                break
    return records


def search_archive(
    *,
    market_id: str | None = None,
    market_slug: str | None = None,
    direction: str | None = None,
    risk: str | None = None,
    dry_run: bool | None = None,
    archive_path: Path = DEFAULT_ARCHIVE_PATH,
) -> list[dict[str, Any]]:
    """Search archived decisions by market and reasoning fields."""
    matches: list[dict[str, Any]] = []
    for record in load_decisions(archive_path):
        decision = record.get("reasoning_decision", {})
        if market_id is not None and record.get("market_id") != market_id:
            continue
        if market_slug is not None and record.get("market_slug") != market_slug:
            continue
        if direction is not None and decision.get("direction") != direction:
            continue
        if risk is not None and decision.get("risk") != risk:
            continue
        if dry_run is not None and record.get("dry_run") is not dry_run:
            continue
        matches.append(record)
    return matches


def archive_statistics(archive_path: Path = DEFAULT_ARCHIVE_PATH) -> dict[str, int | str | None]:
    """Summarize archived decision records."""
    records = load_decisions(archive_path)
    timestamps = [
        record.get("timestamp") for record in records if isinstance(record.get("timestamp"), int)
    ]
    market_ids = {record.get("market_id") for record in records if record.get("market_id")}
    dry_run_count = sum(1 for record in records if record.get("dry_run") is True)

    return {
        "decision_count": len(records),
        "market_count": len(market_ids),
        "first_timestamp": min(timestamps) if timestamps else None,
        "last_timestamp": max(timestamps) if timestamps else None,
        "dry_run_count": dry_run_count,
        "archive_path": str(archive_path),
    }
