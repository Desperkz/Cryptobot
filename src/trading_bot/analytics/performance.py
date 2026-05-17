from __future__ import annotations

from decimal import Decimal
from statistics import mean
from typing import Any

from trading_bot.models import PerformanceSnapshot, to_decimal


class PerformanceAnalyzer:
    def summarize(self, trades: list[dict[str, Any]], symbol: str | None = None) -> PerformanceSnapshot:
        filtered = [trade for trade in trades if symbol is None or trade.get("symbol") == symbol]
        if not filtered:
            return PerformanceSnapshot(symbol, 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))

        r_values = [_trade_r_multiple(trade) for trade in filtered]
        wins = [value for value in r_values if value > 0]
        losses = [abs(value) for value in r_values if value < 0]
        winrate = Decimal(len(wins)) / Decimal(len(r_values))
        expectancy = sum(r_values, Decimal("0")) / Decimal(len(r_values))
        gross_profit = sum(wins, Decimal("0"))
        gross_loss = sum(losses, Decimal("0"))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
        max_drawdown = _drawdown_pct([to_decimal(trade.get("realized_pnl", "0")) for trade in filtered])
        return PerformanceSnapshot(
            symbol=symbol,
            trades=len(filtered),
            winrate=winrate,
            expectancy_r=expectancy,
            profit_factor=profit_factor,
            avg_r_multiple=to_decimal(mean(float(value) for value in r_values)),
            max_drawdown_pct=max_drawdown,
        )

    def symbol_is_healthy(
        self,
        trades: list[dict[str, Any]],
        symbol: str,
        min_trades: int,
        min_expectancy_r: Decimal,
        min_winrate: Decimal,
    ) -> bool:
        snapshot = self.summarize(trades, symbol)
        if snapshot.trades < min_trades:
            return True
        return snapshot.expectancy_r >= min_expectancy_r and snapshot.winrate >= min_winrate


def _trade_r_multiple(trade: dict[str, Any]) -> Decimal:
    pnl = to_decimal(trade.get("realized_pnl", "0"))
    risk = to_decimal(trade.get("risk_amount", trade.get("initial_risk", "0") or "0"))
    if risk <= 0:
        entry = to_decimal(trade.get("entry_price", "0"))
        stop = to_decimal(trade.get("stop_loss", "0"))
        qty = to_decimal(trade.get("quantity", "0"))
        risk = abs(entry - stop) * qty
    return pnl / risk if risk > 0 else Decimal("0")


def _drawdown_pct(pnl_values: list[Decimal]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    drawdown = Decimal("0")
    for pnl in pnl_values:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            drawdown = min(drawdown, (equity - peak) / peak * Decimal("100"))
    return drawdown

