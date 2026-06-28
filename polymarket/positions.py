"""Position helpers."""

from __future__ import annotations

from typing import Any

from .client import build_client


def get_position_size(token_id: str) -> float:
    """Return current position size for token id, or 0 when unavailable."""
    client = build_client()
    if not client.settings.public_key:
        return 0.0

    data: Any = client.get_json(
        f"{client.gamma_base_url}/positions",
        {"user": client.settings.public_key, "token_id": token_id},
    )
    if isinstance(data, list):
        if not data:
            return 0.0
        position = data[0]
    elif isinstance(data, dict):
        positions = data.get("positions") or data.get("data")
        position = positions[0] if isinstance(positions, list) and positions else data
    else:
        return 0.0

    for key in ("size", "quantity", "balance"):
        if key in position:
            try:
                return float(position[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0
