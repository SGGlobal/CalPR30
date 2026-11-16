"""Order helpers with dry-run first behavior."""

from __future__ import annotations

import uuid
from typing import Any

from .client import build_client
from .logger import log_event

_LIVE_ORDER_REFUSAL = (
    "Live order signing is intentionally not implemented. "
    "Keep DRY_RUN=True until an audited signer is added."
)


def place_limit_order(
    token_id: str, side: str, price: float, size: float, neg_risk: bool = False
) -> dict[str, Any]:
    """Place a limit order, or simulate it when DRY_RUN is enabled."""
    client = build_client()
    payload = {
        "token_id": token_id,
        "side": side.upper(),
        "price": round(float(price), 4),
        "size": round(float(size), 4),
        "neg_risk": bool(neg_risk),
    }

    if client.settings.dry_run:
        order_id = f"dry-run-{uuid.uuid4()}"
        log_event("dry_run_order", order_id=order_id, order=payload)
        return {"id": order_id, "status": "dry_run", "payload": payload}

    log_event("live_order_attempt", order=payload)
    raise NotImplementedError(_LIVE_ORDER_REFUSAL)


def cancel_token_orders(token_id: str) -> dict[str, Any]:
    """Cancel open orders for a token id."""
    client = build_client()
    if client.settings.dry_run:
        log_event("dry_run_cancel", token_id=token_id)
        return {"status": "dry_run", "token_id": token_id}
    raise NotImplementedError(_LIVE_ORDER_REFUSAL)


def get_order_status(order_id: str) -> dict[str, Any]:
    """Return order status by id."""
    if order_id.startswith("dry-run-"):
        return {"id": order_id, "status": "dry_run"}
    raise NotImplementedError(_LIVE_ORDER_REFUSAL)
