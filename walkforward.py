"""
Walk-Forward Validation — бот v2
=================================
Тестирует реальные стратегии MEAN_REVERSION и SQUEEZE_BREAKOUT
на исторических данных со скользящим окном.

Схема:
  [====== train 6m ======][== test 2m ==]
                  [====== train 6m ======][== test 2m ==]
                          ...

Использование:
    cd /root/bot_v2
    python walkforward.py --symbols SOLUSDT BNBUSDT BTCUSDT --equity 250

CSV должны лежать в data/:
    data/SOLUSDT_15m.csv  data/SOLUSDT_1h.csv  data/SOLUSDT_4h.csv
    data/BNBUSDT_15m.csv  ...

Скачать свежие данные:
    python download_candles.py  (из папки bot_v2)
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Добавляем src в путь если запускаем напрямую
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent / "src"))

from trading_bot.config import load_config
from trading_bot.backtester.realistic_execution import (
    ExecutionAssumptions,
    SimulatedExecution,
    estimate_quantity_for_risk,
    simulate_realistic_trade,
)
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import Candle, Direction, MarketMetrics, Signal, SymbolFilters, to_decimal
from trading_bot.strategy_engine.edge import EdgeAnalyzer
from trading_bot.strategy_engine.mean_reversion import MeanReversionStrategy
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy
from trading_bot.trade_manager.exit_plan import ExitPlanBuilder


# ---------------------------------------------------------------------------
# Конфигурация окон
# ---------------------------------------------------------------------------
TRAIN_BARS_1H = 24 * 30 * 6   # 6 месяцев на 1h
TEST_BARS_1H  = 24 * 30 * 2   # 2 месяца на 1h
STEP_BARS_1H  = 24 * 30 * 1   # сдвиг 1 месяц

TRAIN_BARS_15M = TRAIN_BARS_1H * 4
TEST_BARS_15M  = TEST_BARS_1H * 4
STEP_BARS_15M  = STEP_BARS_1H * 4

TRAIN_BARS_4H = TRAIN_BARS_1H // 4
TEST_BARS_4H  = TEST_BARS_1H // 4
STEP_BARS_4H  = STEP_BARS_1H // 4


# ---------------------------------------------------------------------------
# Загрузка CSV
# ---------------------------------------------------------------------------
def load_csv(path: Path) -> list[Candle]:
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            candles.append(Candle(
                open_time=int(row.get("open_time") or 0),
                open=to_decimal(row["open"]),
                high=to_decimal(row["high"]),
                low=to_decimal(row["low"]),
                close=to_decimal(row["close"]),
                volume=to_decimal(row.get("volume", "0")),
                close_time=int(row.get("close_time") or row.get("open_time") or 0),
                quote_volume=to_decimal(row.get("quote_volume", "0")),
            ))
    return candles


def load_metric_overrides(path: Path, symbol: str) -> dict[int, MarketMetrics]:
    metrics_by_open_time: dict[int, MarketMetrics] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return metrics_by_open_time
        metric_columns = {
            "spread_bps",
            "top_book_liquidity_usdt",
            "book_liquidity_usdt",
            "funding_rate",
            "funding_rate_per_8h",
            "open_interest",
            "open_interest_change_pct",
            "oi_change_pct",
            "order_book_imbalance",
            "taker_buy_ratio",
            "aggressive_buy_sell_delta",
            "delta",
            "quote_volume_24h",
        }
        if not metric_columns.intersection(set(reader.fieldnames)):
            return metrics_by_open_time
        for row in reader:
            open_time = _optional_int(row.get("open_time"))
            if open_time is None:
                continue
            metrics_by_open_time[open_time] = _metrics_from_row(row, symbol)
    return metrics_by_open_time


# ---------------------------------------------------------------------------
# Симуляция сделки по свечам
# ---------------------------------------------------------------------------
EXECUTION_ASSUMPTIONS = ExecutionAssumptions(
    taker_fee_bps=Decimal("4"),
    base_slippage_bps=Decimal("5"),
    random_slippage_bps=Decimal("1"),
    funding_bps_per_8h=Decimal("1"),
    pessimistic_intrabar=True,
)


def simulate_trade(
    candles: list[Candle],
    entry_idx: int,
    direction: Direction,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    risk_amount: Decimal,
    quantity: Decimal,
    max_bars: int = 72,
    partial_targets: list[dict[str, Any]] | None = None,
) -> SimulatedExecution:
    """
    Симулирует сделку по единой realistic-модели: partial TP, fees,
    slippage, funding, breakeven/trailing и pessimistic intrabar.
    """
    return simulate_realistic_trade(
        candles,
        entry_idx,
        direction,
        entry_price,
        stop_loss,
        take_profit,
        quantity,
        risk_amount,
        max_bars=max_bars,
        assumptions=EXECUTION_ASSUMPTIONS,
        partial_targets=partial_targets,
    )


def _backtest_filters(symbol: str) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        tick_size=Decimal("0.00000001"),
        step_size=Decimal("0.00000001"),
        min_qty=Decimal("0"),
        min_notional=Decimal("0"),
    )


def _partial_targets_for_signal(symbol: str, signal: Signal, quantity: Decimal, cfg: Any) -> list[dict[str, Any]]:
    builder = ExitPlanBuilder(cfg.trade_management)
    targets = builder.build_targets(signal, quantity, _backtest_filters(symbol))
    return [
        {
            "name": target.name,
            "price": target.price,
            "quantity": target.quantity,
            "move_stop_to_breakeven": target.move_stop_to_breakeven,
            "activate_trailing": target.activate_trailing,
        }
        for target in targets
    ]


def _entry_index_15m(one_hour_index: int, candles_15m: list[Candle]) -> int:
    if not candles_15m:
        return 0
    return min(max(one_hour_index * 4, 0), len(candles_15m) - 1)


# ---------------------------------------------------------------------------
# Результат одного окна
# ---------------------------------------------------------------------------
@dataclass
class WindowResult:
    window_idx: int
    symbol: str
    strategy: str
    train_start: int
    test_start: int
    test_end: int
    trades: int
    wins: int
    pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    max_dd: Decimal

    @property
    def winrate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0

    @property
    def profit_factor(self) -> Decimal:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else self.gross_profit

    @property
    def losses(self) -> int:
        return self.trades - self.wins


# ---------------------------------------------------------------------------
# Фиктивные метрики для стратегий
# ---------------------------------------------------------------------------
def _dummy_metrics(candle: Candle) -> MarketMetrics:
    return MarketMetrics(
        symbol="",
        spread_bps=Decimal("3"),
        top_book_liquidity_usdt=Decimal("1000000"),
        aggressive_buy_sell_delta=Decimal("0"),
        funding_rate=Decimal("0"),
        open_interest=None,
        open_interest_change_pct=None,
        quote_volume_24h=Decimal("0"),
    )


def _metrics_for_candle(
    symbol: str,
    candle: Candle,
    overrides: dict[int, MarketMetrics] | None = None,
) -> MarketMetrics:
    if overrides:
        metrics = overrides.get(candle.open_time)
        if metrics:
            return metrics
    metrics = _dummy_metrics(candle)
    return MarketMetrics(
        symbol=symbol,
        spread_bps=metrics.spread_bps,
        top_book_liquidity_usdt=metrics.top_book_liquidity_usdt,
        aggressive_buy_sell_delta=metrics.aggressive_buy_sell_delta,
        funding_rate=metrics.funding_rate,
        open_interest=metrics.open_interest,
        open_interest_change_pct=metrics.open_interest_change_pct,
        quote_volume_24h=metrics.quote_volume_24h,
    )


def _metrics_from_row(row: dict[str, str], symbol: str) -> MarketMetrics:
    quote_volume_24h = _optional_decimal(row, "quote_volume_24h", "quote_volume") or Decimal("0")
    spread_bps = _optional_decimal(row, "spread_bps") or Decimal("3")
    top_book_liquidity = (
        _optional_decimal(row, "top_book_liquidity_usdt", "book_liquidity_usdt")
        or Decimal("1000000")
    )
    return MarketMetrics(
        symbol=symbol,
        quote_volume_24h=quote_volume_24h,
        spread_bps=spread_bps,
        top_book_liquidity_usdt=top_book_liquidity,
        funding_rate=_optional_decimal(row, "funding_rate", "funding_rate_per_8h"),
        open_interest=_optional_decimal(row, "open_interest"),
        open_interest_change_pct=_optional_decimal(row, "open_interest_change_pct", "oi_change_pct"),
        order_book_imbalance=_optional_decimal(row, "order_book_imbalance") or Decimal("0"),
        taker_buy_ratio=_optional_decimal(row, "taker_buy_ratio"),
        aggressive_buy_sell_delta=_optional_decimal(row, "aggressive_buy_sell_delta", "delta") or Decimal("0"),
    )


def _optional_decimal(row: dict[str, str], *names: str) -> Decimal | None:
    for name in names:
        raw = row.get(name)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or text.lower() in {"none", "null", "nan"}:
            continue
        try:
            return Decimal(text)
        except Exception:
            continue
    return None


def _optional_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Тест одного окна
# ---------------------------------------------------------------------------
def test_window(
    symbol: str,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    test_start_1h: int,
    test_end_1h: int,
    mr_strategy: MeanReversionStrategy,
    sqz_strategy: SqueezeBreakoutStrategy,
    equity: Decimal,
    window_idx: int,
    train_start: int,
    cfg: Any,
    metrics_by_open_time: dict[int, MarketMetrics] | None = None,
) -> list[WindowResult]:
    results = []
    risk_pct = Decimal("0.02")

    for strategy_name, strategy in [("MEAN_REVERSION", mr_strategy), ("SQUEEZE_BREAKOUT", sqz_strategy)]:
        trades = wins = 0
        pnl = gross_profit = gross_loss = Decimal("0")
        balance = equity
        peak = balance
        max_dd = Decimal("0")

        # Минимальный контекст для индикаторов
        context = 250
        skip_until = 0

        for i in range(test_start_1h + context, test_end_1h):
            if i < skip_until:
                continue
            # Берём скользящее окно свечей
            c1h = candles_1h[max(0, i - 500): i + 1]
            c4h = candles_4h[max(0, (i // 4) - 250): (i // 4) + 1]
            c15m = candles_15m[max(0, i * 4 - 500): i * 4 + 1]

            if len(c1h) < 50 or len(c4h) < 50 or len(c15m) < 50:
                continue

            metrics = _metrics_for_candle(symbol, candles_1h[i], metrics_by_open_time)

            try:
                signal = strategy.generate(symbol, c15m, c1h, c4h, metrics)
            except Exception:
                continue

            if signal is None:
                continue

            # Размер позиции: фиксированный % от баланса
            entry = signal.entry_price
            stop = signal.stop_loss
            if stop is None or entry <= 0:
                continue
            stop_dist = abs(entry - stop)
            if stop_dist <= 0:
                continue
            risk_amount = balance * risk_pct

            sim_candles = candles_15m
            sim_idx = _entry_index_15m(i, candles_15m)
            sim_max_bars = 72 * 4
            quantity = estimate_quantity_for_risk(
                signal.entry_price,
                signal.stop_loss,
                risk_amount,
                EXECUTION_ASSUMPTIONS,
            )
            if quantity <= 0:
                continue
            partial_targets = _partial_targets_for_signal(symbol, signal, quantity, cfg)
            execution = simulate_trade(
                candles=sim_candles,
                entry_idx=sim_idx,
                direction=signal.direction,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                risk_amount=risk_amount,
                quantity=quantity,
                max_bars=sim_max_bars,
                partial_targets=partial_targets,
            )
            trade_pnl = execution.net_pnl

            skip_until = max(i + 1, execution.exit_index // 4 + 1)
            trades += 1
            pnl += trade_pnl
            balance += trade_pnl
            if trade_pnl > 0:
                wins += 1
                gross_profit += trade_pnl
            else:
                gross_loss += abs(trade_pnl)

            peak = max(peak, balance)
            if peak > 0:
                dd = (balance - peak) / peak * Decimal("100")
                max_dd = min(max_dd, dd)

            # Досрочно останавливаем если просадка >20%
            if max_dd < Decimal("-20"):
                break

        results.append(WindowResult(
            window_idx=window_idx,
            symbol=symbol,
            strategy=strategy_name,
            train_start=train_start,
            test_start=test_start_1h,
            test_end=test_end_1h,
            trades=trades,
            wins=wins,
            pnl=pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            max_dd=max_dd,
        ))

    return results


# ---------------------------------------------------------------------------
# Walk-Forward для одного символа
# ---------------------------------------------------------------------------
def walk_forward_symbol(
    symbol: str,
    data_dir: Path,
    equity: Decimal,
    cfg: Any,
) -> list[WindowResult]:
    p15 = data_dir / f"{symbol}_15m.csv"
    p1h = data_dir / f"{symbol}_1h.csv"
    p4h = data_dir / f"{symbol}_4h.csv"

    for p in [p15, p1h, p4h]:
        if not p.exists():
            print(f"  Файл не найден: {p} — пропускаем {symbol}")
            return []

    print(f"  Загружаем данные {symbol}...")
    c15m = load_csv(p15)
    c1h  = load_csv(p1h)
    c4h  = load_csv(p4h)
    metrics_by_open_time = load_metric_overrides(p1h, symbol)
    if metrics_by_open_time:
        print(f"  market metrics: {len(metrics_by_open_time)} rows from {p1h.name}")
    else:
        print("  market metrics: not found in CSV, using neutral placeholders")

    print(f"  {symbol}: 15m={len(c15m)} 1h={len(c1h)} 4h={len(c4h)} баров")

    if len(c1h) < TRAIN_BARS_1H + TEST_BARS_1H + 300:
        print(f"  Недостаточно данных для {symbol}, нужно минимум {TRAIN_BARS_1H + TEST_BARS_1H + 300} баров 1h")
        return []

    regime = MarketRegimeDetector(cfg.strategy)
    mr = MeanReversionStrategy(cfg.strategy, regime)
    sqz = SqueezeBreakoutStrategy(cfg.strategy, regime)

    all_results: list[WindowResult] = []
    window_idx = 0
    pos = 0

    while pos + TRAIN_BARS_1H + TEST_BARS_1H < len(c1h):
        train_start = pos
        test_start  = pos + TRAIN_BARS_1H
        test_end    = test_start + TEST_BARS_1H

        if test_end > len(c1h):
            break

        print(f"  Окно {window_idx + 1}: train={train_start}-{test_start}, test={test_start}-{test_end}")

        results = test_window(
            symbol=symbol,
            candles_15m=c15m,
            candles_1h=c1h,
            candles_4h=c4h,
            test_start_1h=test_start,
            test_end_1h=test_end,
            mr_strategy=mr,
            sqz_strategy=sqz,
            equity=equity,
            window_idx=window_idx,
            train_start=train_start,
            cfg=cfg,
            metrics_by_open_time=metrics_by_open_time,
        )
        all_results.extend(results)
        window_idx += 1
        pos += STEP_BARS_1H

    return all_results


# ---------------------------------------------------------------------------
# Итоговый отчёт
# ---------------------------------------------------------------------------
def print_report(all_results: list[WindowResult]) -> None:
    if not all_results:
        print("\nНет результатов.")
        return

    print("\n" + "=" * 80)
    print("WALK-FORWARD РЕЗУЛЬТАТЫ")
    print("=" * 80)

    # По стратегии и символу
    from itertools import groupby
    key = lambda r: (r.strategy, r.symbol)
    sorted_results = sorted(all_results, key=key)

    for (strategy, symbol), group in groupby(sorted_results, key=key):
        rows = list(group)
        total_trades = sum(r.trades for r in rows)
        total_wins = sum(r.wins for r in rows)
        total_pnl = sum(r.pnl for r in rows)
        total_gp = sum(r.gross_profit for r in rows)
        total_gl = sum(r.gross_loss for r in rows)
        avg_dd = sum(r.max_dd for r in rows) / len(rows) if rows else Decimal("0")
        worst_dd = min(r.max_dd for r in rows) if rows else Decimal("0")
        pf = total_gp / total_gl if total_gl > 0 else total_gp
        wr = total_wins / total_trades * 100 if total_trades > 0 else 0

        print(f"\n{strategy} | {symbol}")
        print(f"  Окон: {len(rows)} | Сделок: {total_trades} | WR: {wr:.1f}% | PF: {float(pf):.2f}")
        print(f"  PnL: {float(total_pnl):+.2f} USDT | Ср.DD: {float(avg_dd):.1f}% | Worst DD: {float(worst_dd):.1f}%")

        # Детали по окнам
        print(f"  {'Окно':>5} {'Сделок':>7} {'WR%':>6} {'PF':>6} {'PnL':>10} {'MaxDD%':>8}")
        for r in rows:
            pf_w = r.profit_factor
            print(f"  {r.window_idx + 1:>5} {r.trades:>7} {r.winrate * 100:>5.1f}% {float(pf_w):>6.2f} "
                  f"{float(r.pnl):>+10.2f} {float(r.max_dd):>7.1f}%")

    # Итоговая оценка
    print("\n" + "=" * 80)
    print("ИТОГОВАЯ ОЦЕНКА")
    print("=" * 80)
    for strategy in ["MEAN_REVERSION", "SQUEEZE_BREAKOUT"]:
        rows = [r for r in all_results if r.strategy == strategy]
        if not rows:
            continue
        total_trades = sum(r.trades for r in rows)
        total_wins = sum(r.wins for r in rows)
        total_gp = sum(r.gross_profit for r in rows)
        total_gl = sum(r.gross_loss for r in rows)
        pf = total_gp / total_gl if total_gl > 0 else total_gp
        wr = total_wins / total_trades * 100 if total_trades > 0 else 0
        worst_dd = min(r.max_dd for r in rows) if rows else Decimal("0")
        profitable_windows = sum(1 for r in rows if r.pnl > 0)
        total_windows = len(rows)

        approved = (
            total_trades >= 50
            and wr >= 52
            and float(pf) >= 1.2
            and float(worst_dd) >= -15
            and profitable_windows / total_windows >= 0.6 if total_windows > 0 else False
        )

        status = "✅ ОДОБРЕНО для бумажной торговли" if approved else "❌ НЕ ГОТОВО для live"
        print(f"\n{strategy}: {status}")
        print(f"  Сделок: {total_trades} | WR: {wr:.1f}% | PF: {float(pf):.2f} | "
              f"Worst DD: {float(worst_dd):.1f}% | "
              f"Прибыльных окон: {profitable_windows}/{total_windows}")

    print()


# ---------------------------------------------------------------------------
# Сохранение результатов в CSV
# ---------------------------------------------------------------------------
def save_csv(all_results: list[WindowResult], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "window", "symbol", "strategy", "trades", "wins", "losses",
            "winrate", "profit_factor", "pnl", "max_dd",
        ])
        for r in all_results:
            writer.writerow([
                r.window_idx + 1, r.symbol, r.strategy,
                r.trades, r.wins, r.losses,
                f"{r.winrate:.4f}", f"{float(r.profit_factor):.4f}",
                f"{float(r.pnl):.4f}", f"{float(r.max_dd):.4f}",
            ])
    print(f"Результаты сохранены: {path}")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-Forward Validation для бота v2")
    parser.add_argument("--symbols", nargs="+",
                        default=["SOLUSDT", "BNBUSDT", "BTCUSDT", "XRPUSDT"],
                        help="Символы для тестирования")
    parser.add_argument("--equity", type=float, default=250.0,
                        help="Начальный депозит в USDT")
    parser.add_argument("--data-dir", default="data",
                        help="Папка с CSV файлами")
    parser.add_argument("--output", default="data/walkforward_results.csv",
                        help="Файл для сохранения результатов")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    equity = Decimal(str(args.equity))

    print("Walk-Forward Validation v2")
    print(f"Символы: {args.symbols}")
    print(f"Депозит: ${float(equity):.0f}")
    print(f"Окно: {TRAIN_BARS_1H}h обучение / {TEST_BARS_1H}h тест / {STEP_BARS_1H}h сдвиг")
    print()

    cfg = load_config()
    all_results: list[WindowResult] = []

    for symbol in args.symbols:
        print(f"→ {symbol}")
        results = walk_forward_symbol(symbol, data_dir, equity, cfg)
        all_results.extend(results)

    print_report(all_results)

    if all_results:
        save_csv(all_results, Path(args.output))


if __name__ == "__main__":
    main()
