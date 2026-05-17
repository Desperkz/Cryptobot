from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trading_bot.models import Candle, Direction, Signal, SymbolFilters, TradingStyle, to_decimal
from trading_bot.risk_manager import RiskManager


@dataclass(frozen=True)
class BacktestResult:
    trades: int
    wins: int
    losses: int
    realized_pnl: Decimal
    max_drawdown_pct: Decimal
    profit_factor: Decimal
    approved_for_trading: bool


class BacktestEngine:
    def __init__(self, risk_manager: RiskManager, filters: SymbolFilters) -> None:
        self.risk_manager = risk_manager
        self.filters = filters

    def run_naive_signal_backtest(
        self,
        candles: list[Candle],
        equity_usdt: Decimal,
        min_trades: int = 30,
        min_profit_factor: Decimal = Decimal("1.1"),
        min_max_drawdown_pct: Decimal = Decimal("-25"),
    ) -> BacktestResult:
        """Simple EMA-style smoke backtest.

        This is intentionally conservative and should be replaced with research-grade
        walk-forward tests before any real deployment.
        """
        if len(candles) < 260:
            return BacktestResult(0, 0, 0, Decimal("0"), Decimal("0"), Decimal("0"), False)

        balance = equity_usdt
        peak = balance
        max_drawdown = Decimal("0")
        wins = 0
        losses = 0
        gross_profit = Decimal("0")
        gross_loss = Decimal("0")

        index = 220
        while index < len(candles) - 10:
            current = candles[index]
            prev = candles[index - 20]
            direction = Direction.LONG if current.close > prev.close else Direction.SHORT
            atr_proxy = abs(current.close - prev.close) / Decimal("2")
            if atr_proxy <= 0:
                index += 1
                continue
            if direction == Direction.LONG:
                stop = current.close - atr_proxy
                take = current.close + (atr_proxy * Decimal("1.5"))
            else:
                stop = current.close + atr_proxy
                take = current.close - (atr_proxy * Decimal("1.5"))
            signal = Signal(
                symbol=self.filters.symbol,
                direction=direction,
                style=TradingStyle.INTRADAY,
                entry_price=current.close,
                stop_loss=stop,
                take_profit=take,
                confidence=Decimal("0.5"),
                reason="naive backtest signal",
            )
            try:
                plan = self.risk_manager.calculate_plan(signal, balance, self.filters)
            except Exception:
                index += 1
                continue

            pnl = Decimal("0")
            exit_index = index + 1
            for future in candles[index + 1 : min(index + 40, len(candles))]:
                exit_index += 1
                if direction == Direction.LONG:
                    if future.low <= plan.stop_loss:
                        pnl = -plan.risk_amount
                        break
                    if future.high >= plan.take_profit:
                        pnl = plan.reward_amount
                        break
                else:
                    if future.high >= plan.stop_loss:
                        pnl = -plan.risk_amount
                        break
                    if future.low <= plan.take_profit:
                        pnl = plan.reward_amount
                        break
            balance += pnl
            if pnl > 0:
                wins += 1
                gross_profit += pnl
            elif pnl < 0:
                losses += 1
                gross_loss += abs(pnl)
            peak = max(peak, balance)
            if peak > 0:
                max_drawdown = min(max_drawdown, (balance - peak) / peak * Decimal("100"))
            index = exit_index

        trades = wins + losses
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
        approved = trades >= min_trades and profit_factor >= min_profit_factor and max_drawdown >= min_max_drawdown_pct
        return BacktestResult(trades, wins, losses, balance - equity_usdt, max_drawdown, profit_factor, approved)


def load_candles_csv(path: str | Path) -> list[Candle]:
    candles: list[Candle] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            candles.append(
                Candle(
                    open_time=int(row.get("open_time") or row.get("timestamp") or 0),
                    open=to_decimal(row["open"]),
                    high=to_decimal(row["high"]),
                    low=to_decimal(row["low"]),
                    close=to_decimal(row["close"]),
                    volume=to_decimal(row.get("volume", "0")),
                    close_time=int(row.get("close_time") or row.get("timestamp") or 0),
                    quote_volume=to_decimal(row.get("quote_volume", "0")),
                )
            )
    return candles

