"""Offline edge check: SQZ source vs SQZ + context gate vs TREND_BREAKOUT_4H.

Answers one question before any more gates/cohorts are built: does the
*source* signal have positive expectancy after realistic costs, and does the
4h-context gate (edge_v1) or the 4h trend strategy do better?

* uses the production strategy classes unchanged (no copies);
* entry = OPEN of the bar after the signal bar, stop/targets re-anchored with
  the strategy's distances (same rule as the live/paper rebase fix);
* exits = production ``simulate_realistic_trade``: taker fees, slippage,
  funding buffer, pessimistic stop-first intrabar ordering, config exit
  profiles, BE and ATR trailing;
* one open position per symbol and strategy, 3-bar re-entry pause.

Order-flow, OI and relative-strength gates are NOT replayed (no history for
them) - this measures the raw source and the purely price-based context gate.

Usage (run where fapi.binance.com is reachable, after download_candles.py):

    python scripts/strategy_edge_backtest.py --data-dir data \
        --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT

    # sanity check of the harness itself: on a driftless random walk every
    # strategy must lose roughly its costs (no look-ahead, no free edge)
    python scripts/strategy_edge_backtest.py --synthetic 6 --bars 9000
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.backtester.realistic_execution import (  # noqa: E402
    ExecutionAssumptions,
    simulate_realistic_trade,
)
from trading_bot.config import load_config  # noqa: E402
from trading_bot.market_regime_detector import MarketRegimeDetector  # noqa: E402
from trading_bot.models import Candle, Direction, Signal, to_decimal  # noqa: E402
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy  # noqa: E402
from trading_bot.strategy_engine.trend_breakout_4h import TrendBreakout4hStrategy  # noqa: E402

HOUR_MS = 3_600_000
SQZ_WINDOW_1H = 300
SQZ_WINDOW_4H = 120
TREND_WINDOW_4H = 260
RISK_USDT = Decimal("10")


@dataclass
class TradeResult:
    strategy: str
    symbol: str
    direction: str
    entry_time: int
    r: float
    cost_r: float
    reason: str
    regime: str


# ---------------------------------------------------------------- data ----
def load_csv(path: Path) -> list[Candle]:
    out: list[Candle] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            out.append(
                Candle(
                    open_time=int(float(row["open_time"])),
                    open=to_decimal(row["open"]),
                    high=to_decimal(row["high"]),
                    low=to_decimal(row["low"]),
                    close=to_decimal(row["close"]),
                    volume=to_decimal(row["volume"]),
                    close_time=int(float(row["close_time"])),
                )
            )
    out.sort(key=lambda c: c.open_time)
    return out


def aggregate(candles: list[Candle], factor: int) -> list[Candle]:
    out = []
    for start in range(0, len(candles) - factor + 1, factor):
        chunk = candles[start : start + factor]
        out.append(
            Candle(
                open_time=chunk[0].open_time,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum((c.volume for c in chunk), Decimal("0")),
                close_time=chunk[-1].close_time,
            )
        )
    return out


def synthetic_hourly(seed: int, bars: int, start_price: float = 100.0) -> list[Candle]:
    """Driftless random walk with GARCH-like volatility clustering."""
    rng = random.Random(seed)
    t0 = 1_672_531_200_000  # 2023-01-01, aligned to 4h
    price = start_price
    vol = 0.008
    candles = []
    for i in range(bars):
        vol = max(0.002, min(0.03, 0.97 * vol + 0.03 * 0.008 + 0.15 * vol * rng.gauss(0, 1) ** 2 - 0.15 * vol))
        o = price
        path = [o]
        for _ in range(6):
            path.append(path[-1] * math.exp(rng.gauss(0, vol / math.sqrt(6))))
        c = path[-1]
        price = c
        candles.append(
            Candle(
                open_time=t0 + i * HOUR_MS,
                open=Decimal(f"{o:.6f}"),
                high=Decimal(f"{max(path):.6f}"),
                low=Decimal(f"{min(path):.6f}"),
                close=Decimal(f"{c:.6f}"),
                volume=Decimal(f"{1000 * (1 + 40 * vol) * rng.uniform(0.5, 1.5):.2f}"),
                close_time=t0 + (i + 1) * HOUR_MS - 1,
            )
        )
    return candles


# ------------------------------------------------------------ helpers ----
def rebase_to_open(signal: Signal, price: Decimal) -> Signal:
    entry = to_decimal(signal.entry_price)
    stop_d = abs(entry - to_decimal(signal.stop_loss))
    take_d = abs(to_decimal(signal.take_profit) - entry)
    if signal.direction == Direction.LONG:
        return replace(signal, entry_price=price, stop_loss=price - stop_d, take_profit=price + take_d)
    return replace(signal, entry_price=price, stop_loss=price + stop_d, take_profit=price - take_d)


def targets_for(signal: Signal, profile: list[Any], quantity: Decimal) -> list[dict[str, Any]]:
    entry = to_decimal(signal.entry_price)
    stop_d = abs(entry - to_decimal(signal.stop_loss))
    out, allocated = [], Decimal("0")
    for idx, item in enumerate(profile):
        qty = quantity - allocated if idx == len(profile) - 1 else quantity * item.fraction
        allocated += qty
        rr = item.reward_risk
        price = entry + stop_d * rr if signal.direction == Direction.LONG else entry - stop_d * rr
        out.append(
            {
                "name": item.name,
                "price": price,
                "quantity": qty,
                "move_stop_to_breakeven": item.move_stop_to_breakeven,
                "activate_trailing": item.activate_trailing,
            }
        )
    return out


def context_ok(signal: Signal) -> bool:
    meta = signal.metadata or {}
    has_4h_squeeze = int(meta.get("squeeze_bars_4h") or 0) > 0
    regime = str(meta.get("regime") or "").upper()
    return has_4h_squeeze or regime in {"TREND_UP", "TREND_DOWN", "MOMENTUM"}


def run_trade(
    name: str,
    symbol: str,
    signal: Signal,
    exec_candles: list[Candle],
    signal_index: int,
    profile: list[Any],
    trailing_pct: Decimal,
    max_bars: int,
    base: ExecutionAssumptions,
) -> tuple[TradeResult, int] | None:
    if signal_index + 1 >= len(exec_candles):
        return None
    signal = rebase_to_open(signal, exec_candles[signal_index + 1].open)
    entry = to_decimal(signal.entry_price)
    stop = to_decimal(signal.stop_loss)
    stop_d = abs(entry - stop)
    if stop_d <= 0 or stop <= 0:
        return None
    qty = RISK_USDT / stop_d
    assumptions = replace(base, trailing_callback_rate_pct=trailing_pct)
    # The simulator starts evaluating at entry_index + 1, so the entry bar is
    # the bar AFTER the signal: pass signal_index and the next open as entry.
    result = simulate_realistic_trade(
        exec_candles,
        signal_index,
        signal.direction,
        entry,
        stop,
        to_decimal(signal.take_profit),
        qty,
        RISK_USDT,
        max_bars=max_bars,
        assumptions=assumptions,
        partial_targets=targets_for(signal, profile, qty),
    )
    costs = result.fees + result.slippage_cost + result.funding_cost
    return (
        TradeResult(
            strategy=name,
            symbol=symbol,
            direction=signal.direction.value,
            entry_time=exec_candles[signal_index + 1].open_time,
            r=float(result.net_pnl / RISK_USDT),
            cost_r=float(costs / RISK_USDT),
            reason=result.reason,
            regime=str((signal.metadata or {}).get("regime") or ""),
        ),
        result.exit_index,
    )


# ----------------------------------------------------------- backtest ----
def backtest_symbol(symbol: str, c1h: list[Candle], c4h: list[Candle], cfg: Any) -> list[TradeResult]:
    regime = MarketRegimeDetector(cfg.strategy)
    sqz = SqueezeBreakoutStrategy(cfg.strategy, regime)
    trend = TrendBreakout4hStrategy()
    tm = cfg.trade_management
    sqz_profile = tm.strategy_exit_profiles.get("SQUEEZE_BREAKOUT") or tm.partial_take_profits
    trend_profile = tm.strategy_exit_profiles.get("TREND_BREAKOUT_4H") or tm.partial_take_profits
    base = ExecutionAssumptions(
        taker_fee_bps=cfg.risk.taker_fee_bps,
        base_slippage_bps=cfg.risk.slippage_bps,
        funding_bps_per_8h=cfg.risk.funding_buffer_bps,
        breakeven_offset_bps=tm.breakeven_offset_bps,
    )

    four_h_close_index = {c.close_time: idx for idx, c in enumerate(c4h)}
    busy_until: dict[str, int] = defaultdict(lambda: -1)
    results: list[TradeResult] = []
    j4 = -1  # index of last CLOSED 4h candle
    for i in range(SQZ_WINDOW_1H, len(c1h) - 1):
        now = c1h[i].close_time
        while j4 + 1 < len(c4h) and c4h[j4 + 1].close_time <= now:
            j4 += 1
        if j4 < TREND_WINDOW_4H:
            continue
        w1h = c1h[i - SQZ_WINDOW_1H + 1 : i + 1]
        w15 = [c1h[i]]  # only close_time is read from the 15m frame

        # --- SQZ source and edge_v1 context variant -----------------------
        if busy_until["SQZ_RAW"] < i or busy_until["SQZ_CONTEXT"] < i:
            signal = sqz.generate(symbol, w15, w1h, c4h[j4 - SQZ_WINDOW_4H + 1 : j4 + 1], None)
            if signal is not None:
                atr_pct = to_decimal(signal.metadata.get("atr_pct") or "0.4")
                trail = max(Decimal("0.4"), min(atr_pct, Decimal("2.0")))
                for name, admit in (("SQZ_RAW", True), ("SQZ_CONTEXT", context_ok(signal))):
                    if admit and busy_until[name] < i:
                        done = run_trade(name, symbol, signal, c1h, i, sqz_profile, trail, 72, base)
                        if done:
                            results.append(done[0])
                            busy_until[name] = done[1] + 3

        # --- 4h trend: evaluate only right after a 4h close ---------------
        if now in four_h_close_index and busy_until["TREND_BREAKOUT_4H"] < i:
            signal = trend.generate(symbol, w15, [], c4h[j4 - TREND_WINDOW_4H + 1 : j4 + 1], None, now_ms=now)
            if signal is not None:
                trail = to_decimal(signal.metadata["trailing_callback_rate_pct"])
                done = run_trade("TREND_BREAKOUT_4H", symbol, signal, c1h, i, trend_profile, trail, 24 * 60, base)
                if done:
                    results.append(done[0])
                    busy_until["TREND_BREAKOUT_4H"] = done[1] + 3
    return results


# ------------------------------------------------------------- report ----
def summarize(rows: list[TradeResult]) -> dict[str, float]:
    if not rows:
        return {"n": 0}
    rs = [r.r for r in rows]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for row in sorted(rows, key=lambda r: r.entry_time):
        equity += row.r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    mean = sum(rs) / len(rs)
    sd = (sum((x - mean) ** 2 for x in rs) / max(1, len(rs) - 1)) ** 0.5
    months = max(1.0, (max(r.entry_time for r in rows) - min(r.entry_time for r in rows)) / (30 * 86_400_000))
    return {
        "n": len(rs),
        "win": len(wins) / len(rs),
        "avg_r": mean,
        "ci95": 1.96 * sd / math.sqrt(len(rs)),
        "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
        "total_r": sum(rs),
        "max_dd_r": max_dd,
        "cost_r": sum(r.cost_r for r in rows) / len(rows),
        "trades_per_month": len(rs) / months,
        "best_r": max(rs),
    }


def print_report(results: list[TradeResult]) -> None:
    by_strategy: dict[str, list[TradeResult]] = defaultdict(list)
    for row in results:
        by_strategy[row.strategy].append(row)
    header = f"{'strategy':<20}{'n':>6}{'win%':>7}{'avgR':>8}{'±95%':>7}{'PF':>6}{'totR':>8}{'maxDD':>8}{'costR':>7}{'/mo':>6}{'best':>7}"
    print(header)
    print("-" * len(header))
    for name in ("SQZ_RAW", "SQZ_CONTEXT", "TREND_BREAKOUT_4H"):
        s = summarize(by_strategy.get(name, []))
        if not s.get("n"):
            print(f"{name:<20}{0:>6}")
            continue
        print(
            f"{name:<20}{s['n']:>6}{s['win']*100:>6.1f}%{s['avg_r']:>8.3f}{s['ci95']:>7.3f}{s['pf']:>6.2f}"
            f"{s['total_r']:>8.1f}{s['max_dd_r']:>8.1f}{s['cost_r']:>7.3f}{s['trades_per_month']:>6.1f}{s['best_r']:>7.2f}"
        )
    print()
    for name, rows in sorted(by_strategy.items()):
        per_year: dict[int, list[TradeResult]] = defaultdict(list)
        for row in rows:
            per_year[1970 + row.entry_time // (365.25 * 86_400_000)].append(row)
        years = ", ".join(
            f"{int(y)}: n={len(v)} avgR={summarize(v)['avg_r']:+.3f}" for y, v in sorted(per_year.items())
        )
        print(f"{name:<20} by year -> {years}")
    print()
    print("avgR is net of fees/slippage/funding, in units of initial risk.")
    print("±95% is the half-width of an approximate CI; if it spans 0, the edge is not established.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--symbols", nargs="*", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--synthetic", type=int, default=0, help="N synthetic random-walk symbols instead of CSV")
    parser.add_argument("--bars", type=int, default=9000, help="hourly bars per synthetic symbol")
    parser.add_argument("--csv-out", default="")
    args = parser.parse_args()

    cfg = load_config(args.config)
    results: list[TradeResult] = []
    if args.synthetic:
        for k in range(args.synthetic):
            c1h = synthetic_hourly(seed=1000 + k, bars=args.bars)
            results += backtest_symbol(f"SYN{k}", c1h, aggregate(c1h, 4), cfg)
            print(f"SYN{k}: {len(results)} trades so far", file=sys.stderr)
    else:
        data = Path(args.data_dir)
        for symbol in args.symbols:
            p1h, p4h = data / f"{symbol}_1h.csv", data / f"{symbol}_4h.csv"
            if not p1h.exists():
                print(f"skip {symbol}: {p1h} missing", file=sys.stderr)
                continue
            c1h = load_csv(p1h)
            c4h = load_csv(p4h) if p4h.exists() else aggregate(c1h, 4)
            results += backtest_symbol(symbol, c1h, c4h, cfg)
            print(f"{symbol}: {len(results)} trades so far", file=sys.stderr)

    print_report(results)
    if args.csv_out:
        with open(args.csv_out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["strategy", "symbol", "direction", "entry_time", "r", "cost_r", "reason", "regime"])
            for row in results:
                writer.writerow([row.strategy, row.symbol, row.direction, row.entry_time, f"{row.r:.4f}",
                                 f"{row.cost_r:.4f}", row.reason, row.regime])


if __name__ == "__main__":
    main()
