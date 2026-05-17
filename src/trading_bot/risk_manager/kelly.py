from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_bot.config import RiskConfig
from trading_bot.models import to_decimal


class KellyRiskSizer:
    """Conservative fractional Kelly position-risk scaler.

    It uses recent realized R multiples. If there is not enough information, it
    returns the base risk from config. The result is always clipped by min/max
    risk caps, so Kelly cannot recreate the old 10% risk profile.
    """

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def risk_pct(self, trades: list[dict[str, Any]]) -> Decimal:
        base = self.config.risk_per_trade_pct
        if not self.config.adaptive_kelly_enabled:
            return base

        recent = trades[: self.config.kelly_lookback_trades]
        r_values = [_r_multiple(trade) for trade in recent if _r_multiple(trade) != 0]
        if len(r_values) < 10:
            return _clip(base, self.config.kelly_min_risk_pct, self.config.kelly_max_risk_pct)

        wins = [r for r in r_values if r > 0]
        losses = [abs(r) for r in r_values if r < 0]
        if not wins or not losses:
            return _clip(base, self.config.kelly_min_risk_pct, self.config.kelly_max_risk_pct)

        winrate = Decimal(len(wins)) / Decimal(len(r_values))
        avg_win = sum(wins, Decimal("0")) / Decimal(len(wins))
        avg_loss = sum(losses, Decimal("0")) / Decimal(len(losses))
        payoff = avg_win / avg_loss if avg_loss > 0 else Decimal("0")
        if payoff <= 0:
            return self.config.kelly_min_risk_pct

        full_kelly = winrate - ((Decimal("1") - winrate) / payoff)
        if full_kelly <= 0:
            return self.config.kelly_min_risk_pct
        fractional = full_kelly * self.config.kelly_fraction
        return _clip(fractional, self.config.kelly_min_risk_pct, self.config.kelly_max_risk_pct)


def _r_multiple(trade: dict[str, Any]) -> Decimal:
    value = trade.get("r_multiple")
    if value not in (None, "", "0", 0):
        return to_decimal(value)
    pnl = to_decimal(trade.get("realized_pnl", "0") or "0")
    risk = to_decimal(trade.get("risk_amount", "0") or "0")
    return pnl / risk if risk > 0 else Decimal("0")


def _clip(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(value, upper))

