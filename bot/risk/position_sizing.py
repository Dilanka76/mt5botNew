"""Fixed lot size lookup by current account balance (not risk-% based)."""
from __future__ import annotations

from bot.config import PositionSizingTier


def calculate_lots(balance: float, tiers: list[PositionSizingTier]) -> float:
    """Returns the lot size for the first tier whose max_balance >= balance.

    Tiers must be ordered ascending by max_balance, with the last tier's
    max_balance set to None (no upper bound).
    """
    for tier in tiers:
        if tier.max_balance is None or balance <= tier.max_balance:
            return tier.lots
    return tiers[-1].lots
