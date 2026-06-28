"""Risk and sizing helpers."""

from __future__ import annotations


def calculate_shares(usd_amount: float, price: float) -> float:
    """Calculate shares purchasable for a USD amount at a given price."""
    if price <= 0:
        raise ValueError("price must be greater than zero")
    return round(usd_amount / price, 4)
