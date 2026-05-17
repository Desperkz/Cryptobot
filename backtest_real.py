"""
Полноценный бэктест на реальной стратегии бота.

Использование:
    python backtest_real.py --equity 250

CSV-файлы должны лежать в папке data/ рядом со скриптом:
    data/BTCUSDT_15m.csv
    data/BTCUSDT_1h.csv
    data/BTCUSDT_4h.csv
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
# Минимальные копии моделей (не требует установки бота)
# ---------------------------------------------------------------------------

def to_decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


@dataclass
class Candle:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int = 0
    quote_volume: Decimal = Decimal("0")


def load_csv(path: str) -> list[Candle]:
    candles = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(Candle(
                open_time=int(row.get("open_time") or row.get("timestamp") or 0),
                open=to_decimal(row["open"]),
                high=to_decimal(row["high"]),
                low=to_decimal(row["low"]),
                close=to_decimal(row["close"]),
                volume=to_decimal(row.get("volume", "0")),
                close_time=int(row.get("close_time") or row.get("open_time") or 0),
                quote_volume=to_decimal(row.get("quote_volume", "0")),
            ))
    return candles


# ---------------------------------------------------------------------------
# Индикаторы
# ---------------------------------------------------------------------------

def ema(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        return [values[-1]] * len(values) if values else []
    k = Decimal("2") / Decimal(period + 1)
    result = [sum(values[:period], Decimal("0")) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (Decimal("1") - k))
    # Выровняем длину
    pad = [result[0]] * (len(values) - len(result))
    return pad + result


def rsi(values: list[Decimal], period: int = 14) -> Decimal:
    if len(values) < period + 1:
        return Decimal("50")
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[-period - 1 + i] - values[-period - 1 + i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = sum(gains, Decimal("0")) / period
    avg_loss = sum(losses, Decimal("0")) / period
    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return Decimal("100") - Decimal("100") / (Decimal("1") + rs)


def atr(candles: list[Candle], period: int = 14) -> Decimal:
    if len(candles) < period + 1:
        return candles[-1].high - candles[-1].low if candles else Decimal("1")
    trs = []
    for i in range(1, period + 1):
        c = candles[-period - 1 + i]
        prev_close = candles[-period - 1 + i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    return sum(trs, Decimal("0")) / period


def volume_ratio(candles: list[Candle], lookback: int = 20) -> Decimal:
    vols = [c.volume for c in candles[-lookback - 1:-1]]
    if not vols:
        return Decimal("1")
    avg = sum(vols, Decimal("0")) / len(vols)
    return candles[-1].volume / avg if avg > 0 else Decimal("1")


def higher_high_higher_low(candles: list[Candle], n: int = 3) -> bool:
    if len(candles) < n * 2:
        return False
    recent = candles[-n:]
    prev = candles[-n * 2:-n]
    return (max(c.high for c in recent) > max(c.high for c in prev) and
            min(c.low for c in recent) > min(c.low for c in prev))


def lower_high_lower_low(candles: list[Candle], n: int = 3) -> bool:
    if len(candles) < n * 2:
        return False
    recent = candles[-n:]
    prev = candles[-n * 2:-n]
    return (max(c.high for c in recent) < max(c.high for c in prev) and
            min(c.low for c in recent) < min(c.low for c in prev))


# ---------------------------------------------------------------------------
# Определение режима рынка
# ---------------------------------------------------------------------------

def detect_regime(candles_4h: list[Candle]) -> str:
    closes = [c.close for c in candles_4h]
    if len(closes) < 50:
        return "RANGE"
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    price = closes[-1]
    if price > e20 > e50:
        return "TREND_UP"
    if price < e20 < e50:
        return "TREND_DOWN"
    return "RANGE"


# ---------------------------------------------------------------------------
# Генерация сигналов
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    direction: str  # LONG / SHORT
    strategy: str   # TREND / MEAN_REVERSION
    entry: Decimal
    stop: Decimal
    take_profit: Decimal
    style: str = "INTRADAY"


def trend_signal(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
) -> Signal | None:
    MIN = 210
    if len(candles_15m) < MIN or len(candles_1h) < MIN or len(candles_4h) < MIN:
        return None

    regime = detect_regime(candles_4h)

    closes_4h = [c.close for c in candles_4h]
    e200_4h = ema(closes_4h, 200)[-1]
    price_4h = candles_4h[-1].close

    closes_1h = [c.close for c in candles_1h]
    e20_1h = ema(closes_1h, 20)[-1]
    e50_1h = ema(closes_1h, 50)[-1]

    closes_15m = [c.close for c in candles_15m]
    e20_15m = ema(closes_15m, 20)[-1]
    e50_15m = ema(closes_15m, 50)[-1]
    rsi_15m = rsi(closes_15m)
    atr_15m = atr(candles_15m)
    vol_ratio = volume_ratio(candles_15m)
    entry = candles_15m[-1].close

    if entry <= 0 or atr_15m <= 0:
        return None

    atr_pct = atr_15m / entry * 100
    if atr_pct < Decimal("0.15") or atr_pct > Decimal("8.0"):
        return None
    if vol_ratio < Decimal("1.3"):   # строже: было 1.15
        return None

    # Определяем направление — только чистый тренд, без MOMENTUM
    bullish = regime == "TREND_UP" and price_4h > e200_4h
    bearish = regime == "TREND_DOWN" and price_4h < e200_4h

    direction = None
    if (bullish and e20_1h > e50_1h and higher_high_higher_low(candles_1h)
            and entry > e20_15m > e50_15m and Decimal("45") <= rsi_15m <= Decimal("72")):
        direction = "LONG"
    elif (bearish and e20_1h < e50_1h and lower_high_lower_low(candles_1h)
            and entry < e20_15m < e50_15m and Decimal("28") <= rsi_15m <= Decimal("55")):
        direction = "SHORT"

    if not direction:
        return None

    stop_dist = atr_15m * Decimal("1.8")
    rr = Decimal("1.8")

    if direction == "LONG":
        stop = entry - stop_dist
        tp = entry + stop_dist * rr
    else:
        stop = entry + stop_dist
        tp = entry - stop_dist * rr

    if stop <= 0 or tp <= 0:
        return None

    return Signal("LONG" if direction == "LONG" else "SHORT", "TREND", entry, stop, tp)


def mean_reversion_signal(
    candles_15m: list[Candle],
    candles_4h: list[Candle],
) -> Signal | None:
    MIN = 210
    if len(candles_15m) < MIN or len(candles_4h) < MIN:
        return None

    regime = detect_regime(candles_4h)

    closes_4h = [c.close for c in candles_4h]
    e200_4h = ema(closes_4h, 200)[-1]
    atr_4h = atr(candles_4h)
    price_4h = candles_4h[-1].close

    if atr_4h <= 0:
        return None

    deviation = (price_4h - e200_4h) / atr_4h
    threshold = Decimal("2.0")
    if regime in ("TREND_UP", "TREND_DOWN"):
        threshold += Decimal("1.5")

    closes_15m = [c.close for c in candles_15m]
    rsi_15m = rsi(closes_15m)
    entry = candles_15m[-1].close
    atr_15m = atr(candles_15m)

    if atr_15m <= 0:
        return None

    direction = None
    if deviation <= -threshold and rsi_15m <= Decimal("25"):   # строже: было 28
        direction = "LONG"
    elif deviation >= threshold and rsi_15m >= Decimal("75"):  # строже: было 72
        direction = "SHORT"

    if not direction:
        return None

    stop_dist = atr_15m * Decimal("1.0")
    rr = Decimal("1.1")   # быстрее берём прибыль: было 1.4

    if direction == "LONG":
        stop = entry - stop_dist
        tp = entry + stop_dist * rr
    else:
        stop = entry + stop_dist
        tp = entry - stop_dist * rr

    if stop <= 0 or tp <= 0:
        return None

    return Signal(direction, "MEAN_REVERSION", entry, stop, tp)


# ---------------------------------------------------------------------------
# BTC-фильтр и UTC-фильтр
# ---------------------------------------------------------------------------

AVOID_UTC_HOURS = {0, 1, 2, 3, 4, 5, 6, 7}


def btc_filter_ok(signal: Signal, btc_4h_candles: list[Candle]) -> bool:
    if len(btc_4h_candles) < 2:
        return True
    change = (btc_4h_candles[-1].close - btc_4h_candles[-2].close) / btc_4h_candles[-2].close
    # Блокируем лонги при падении BTC > 3% за 4h
    if change <= Decimal("-0.03") and signal.direction == "LONG":
        return False
    # Блокируем шорты при росте BTC > 3% за 4h
    if change >= Decimal("0.03") and signal.direction == "SHORT":
        return False
    return True


def utc_filter_ok(candle: Candle) -> bool:
    hour = (candle.close_time // 3_600_000) % 24
    return hour not in AVOID_UTC_HOURS


def ema200_alignment_ok(signal: Signal, candles_1h: list[Candle]) -> bool:
    """Цена должна быть по направлению сделки относительно EMA200 на 1h."""
    if len(candles_1h) < 205:
        return False
    closes_1h = [c.close for c in candles_1h]
    e200 = ema(closes_1h, 200)[-1]
    price = candles_1h[-1].close
    if signal.direction == "LONG" and price < e200:
        return False
    if signal.direction == "SHORT" and price > e200:
        return False
    return True


def volatility_filter_ok(candles_15m: list[Candle]) -> bool:
    """ATR% должен быть в рабочем диапазоне — не слишком тихо и не слишком шумно."""
    if len(candles_15m) < 20:
        return False
    atr_val = atr(candles_15m)
    price = candles_15m[-1].close
    if price <= 0:
        return False
    atr_pct = atr_val / price * 100
    return Decimal("0.25") <= atr_pct <= Decimal("4.0")


def consecutive_candles_ok(signal: Signal, candles_15m: list[Candle], n: int = 3) -> bool:
    """Последние N свечей должны подтверждать направление (нет разворота прямо сейчас)."""
    if len(candles_15m) < n + 1:
        return True
    recent = candles_15m[-n:]
    if signal.direction == "LONG":
        # Не входим если последняя свеча — большая медвежья (тело > 60% ATR)
        last = recent[-1]
        body = last.open - last.close  # медвежье тело
        atr_val = atr(candles_15m[-20:])
        if body > atr_val * Decimal("0.6") and last.close < last.open:
            return False
    else:
        last = recent[-1]
        body = last.close - last.open  # бычье тело
        atr_val = atr(candles_15m[-20:])
        if body > atr_val * Decimal("0.6") and last.close > last.open:
            return False
    return True


# ---------------------------------------------------------------------------
# Риск-менеджер (упрощённый, соответствует конфигу)
# ---------------------------------------------------------------------------

RISK_PCT = Decimal("0.02")
MAX_DAILY_LOSS_PCT = Decimal("0.06")
FEE_BPS = Decimal("4") * 2     # taker x2
SLIPPAGE_BPS = Decimal("5")
MIN_RR = Decimal("1.2")


def calc_position(signal: Signal, equity: Decimal) -> tuple[Decimal, Decimal] | None:
    """Возвращает (risk_amount, reward_amount) или None если не проходит."""
    stop_dist = abs(signal.entry - signal.stop)
    reward_dist = abs(signal.take_profit - signal.entry)
    if stop_dist <= 0:
        return None
    rr = reward_dist / stop_dist
    if rr < MIN_RR:
        return None
    cost_bps = FEE_BPS + SLIPPAGE_BPS + Decimal("1")
    estimated_cost = stop_dist + (signal.entry * cost_bps / Decimal("10000"))
    risk_budget = equity * RISK_PCT
    qty = risk_budget / estimated_cost
    notional = qty * signal.entry
    if notional < Decimal("5"):
        return None
    risk_amount = qty * estimated_cost
    reward_amount = qty * reward_dist
    return risk_amount, reward_amount


# ---------------------------------------------------------------------------
# Синхронизация таймфреймов
# ---------------------------------------------------------------------------

def build_index(candles: list[Candle]) -> dict[int, int]:
    """open_time -> индекс в списке."""
    return {c.open_time: i for i, c in enumerate(candles)}


def find_higher_tf_index(ts: int, candles_htf: list[Candle]) -> int:
    """Находим последнюю закрытую свечу HTF перед ts."""
    result = -1
    for i, c in enumerate(candles_htf):
        if c.open_time <= ts:
            result = i
        else:
            break
    return result


# ---------------------------------------------------------------------------
# Основной бэктест
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    index: int
    strategy: str
    direction: str
    entry: Decimal
    exit_price: Decimal
    pnl: Decimal
    r_multiple: Decimal
    equity_after: Decimal


def run_backtest(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    starting_equity: Decimal,
) -> tuple[list[TradeRecord], list[Decimal]]:

    equity = starting_equity
    peak = equity
    equity_curve = [float(equity)]
    trades: list[TradeRecord] = []

    daily_loss_start = equity
    last_day = -1
    consecutive_losses = 0
    cooldown_until = -1

    i = 220  # минимум для EMA200
    while i < len(candles_15m) - 40:
        candle = candles_15m[i]

        # UTC-фильтр
        if not utc_filter_ok(candle):
            i += 1
            continue

        # Кулдаун
        if candle.open_time < cooldown_until:
            i += 1
            continue

        # Сброс дневного лимита
        day = candle.open_time // 86_400_000
        if day != last_day:
            daily_loss_start = equity
            last_day = day

        # Дневной лимит убытка
        if equity < daily_loss_start * (1 - MAX_DAILY_LOSS_PCT):
            i += 1
            continue

        # Срез свечей для анализа
        sl_15m = candles_15m[max(0, i - 250): i + 1]

        idx_1h = find_higher_tf_index(candle.open_time, candles_1h)
        idx_4h = find_higher_tf_index(candle.open_time, candles_4h)
        if idx_1h < 220 or idx_4h < 50:
            i += 1
            continue

        sl_1h = candles_1h[max(0, idx_1h - 250): idx_1h + 1]
        sl_4h = candles_4h[max(0, idx_4h - 250): idx_4h + 1]

        # Генерируем сигналы — только Mean Reversion
        sig_trend = None  # временно отключена для анализа
        sig_mr = mean_reversion_signal(sl_15m, sl_4h)

        # Выбираем лучший (или None)
        signal = None
        if sig_trend and sig_mr:
            # Конфликт направлений — пропускаем
            if sig_trend.direction != sig_mr.direction:
                i += 1
                continue
            signal = sig_trend  # оба согласны — берём трендовый
        elif sig_trend:
            signal = sig_trend
        elif sig_mr:
            signal = sig_mr

        if signal is None:
            i += 1
            continue

        # BTC-фильтр (если торгуем не BTC — подставь свои 4h-свечи BTC)
        # Для бэктеста на BTC используем те же 4h
        if not btc_filter_ok(signal, sl_4h):
            i += 1
            continue

        # Доп. фильтры качества сигнала
        if not ema200_alignment_ok(signal, sl_1h):
            i += 1
            continue
        if not volatility_filter_ok(sl_15m):
            i += 1
            continue
        if not consecutive_candles_ok(signal, sl_15m):
            i += 1
            continue

        # Размер позиции
        pos = calc_position(signal, equity)
        if pos is None:
            i += 1
            continue
        risk_amount, reward_amount = pos

        # Симуляция исполнения (следующие 80 свечей = 20 часов)
        pnl = Decimal("0")
        exit_price = signal.entry
        exit_i = i + 1

        for j, future in enumerate(candles_15m[i + 1: min(i + 81, len(candles_15m))]):
            exit_i = i + 1 + j
            if signal.direction == "LONG":
                if future.low <= signal.stop:
                    pnl = -risk_amount
                    exit_price = signal.stop
                    break
                if future.high >= signal.take_profit:
                    pnl = reward_amount
                    exit_price = signal.take_profit
                    break
            else:
                if future.high >= signal.stop:
                    pnl = -risk_amount
                    exit_price = signal.stop
                    break
                if future.low <= signal.take_profit:
                    pnl = reward_amount
                    exit_price = signal.take_profit
                    break

        equity += pnl
        equity_curve.append(float(equity))
        peak = max(peak, equity)

        if pnl < 0:
            consecutive_losses += 1
            if consecutive_losses >= 2:
                # Кулдаун 2 часа = 8 свечей 15m
                cooldown_until = candle.open_time + 2 * 3600 * 1000
        else:
            consecutive_losses = 0

        r_multiple = pnl / risk_amount if risk_amount > 0 else Decimal("0")
        trades.append(TradeRecord(
            index=i,
            strategy=signal.strategy,
            direction=signal.direction,
            entry=signal.entry,
            exit_price=exit_price,
            pnl=pnl,
            r_multiple=r_multiple,
            equity_after=equity,
        ))

        i = exit_i + 1

    return trades, equity_curve


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

def print_report(trades: list[TradeRecord], equity_curve: list[Decimal], starting_equity: Decimal) -> None:
    if not trades:
        print("\n❌ Сделок не найдено. Проверь, что все три CSV лежат в папке data/")
        return

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
    winrate = len(wins) / len(trades) * 100
    avg_r = sum(t.r_multiple for t in trades) / len(trades)
    final_equity = equity_curve[-1]
    total_return = (final_equity - float(starting_equity)) / float(starting_equity) * 100

    # Просадка
    peak = float(starting_equity)
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (eq - peak) / peak * 100
        max_dd = min(max_dd, dd)

    # По стратегиям
    trend_trades = [t for t in trades if t.strategy == "TREND"]
    mr_trades = [t for t in trades if t.strategy == "MEAN_REVERSION"]

    def wr(lst: list) -> str:
        if not lst:
            return "—"
        w = len([t for t in lst if t.pnl > 0])
        return f"{w}/{len(lst)} ({w/len(lst)*100:.0f}%)"

    print("\n" + "=" * 55)
    print("  РЕЗУЛЬТАТЫ БЭКТЕСТА — BTCUSDT")
    print("=" * 55)
    print(f"  Всего сделок       : {len(trades)}")
    print(f"  Wins / Losses      : {len(wins)} / {len(losses)}")
    print(f"  Winrate            : {winrate:.1f}%")
    print(f"  Profit Factor      : {profit_factor:.2f}  {'✅' if profit_factor >= 1.2 else '❌'}")
    print(f"  Avg R-multiple     : {avg_r:.3f}")
    print(f"  Макс. просадка     : {max_dd:.1f}%  {'✅' if max_dd >= -25 else '❌'}")
    print(f"  Стартовый баланс   : ${float(starting_equity):.0f}")
    print(f"  Финальный баланс   : ${final_equity:.2f}")
    print(f"  Итог               : {total_return:+.1f}%")
    print("-" * 55)
    print(f"  TREND_FOLLOWING    : {wr(trend_trades)}")
    print(f"  MEAN_REVERSION     : {wr(mr_trades)}")
    print("=" * 55)

    if profit_factor >= 1.2 and max_dd >= -25 and len(trades) >= 30:
        print("\n✅ Стратегия прошла минимальные критерии.")
        print("   Следующий шаг: PAPER_TRADING 4–8 недель.\n")
    else:
        reasons = []
        if profit_factor < 1.2:
            reasons.append(f"Profit Factor {profit_factor:.2f} < 1.2")
        if max_dd < -25:
            reasons.append(f"Просадка {max_dd:.1f}% > -25%")
        if len(trades) < 30:
            reasons.append(f"Мало сделок ({len(trades)} < 30)")
        print(f"\n❌ Стратегия не прошла критерии: {', '.join(reasons)}")
        print("   Нужна доработка перед live-торговлей.\n")

    # Последние 10 сделок
    print("  Последние 10 сделок:")
    print(f"  {'#':>3}  {'Стратегия':<16} {'Dir':<5} {'PnL':>8}  {'R':>5}  {'Баланс':>10}")
    print("  " + "-" * 52)
    for t in trades[-10:]:
        pnl_str = f"+${float(t.pnl):.2f}" if t.pnl > 0 else f"-${abs(float(t.pnl)):.2f}"
        print(f"  {trades.index(t)+1:>3}  {t.strategy:<16} {t.direction:<5} {pnl_str:>8}  {float(t.r_multiple):>5.2f}  ${float(t.equity_after):>9.2f}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Бэктест реальной стратегии бота")
    parser.add_argument("--equity", type=float, default=250, help="Стартовый баланс USDT")
    parser.add_argument("--data-dir", type=str, default="data", help="Папка с CSV файлами")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    files = {
        "15m": data_dir / "BTCUSDT_15m.csv",
        "1h":  data_dir / "BTCUSDT_1h.csv",
        "4h":  data_dir / "BTCUSDT_4h.csv",
    }

    for tf, path in files.items():
        if not path.exists():
            print(f"❌ Файл не найден: {path}")
            print(f"   Скачай свечи {tf} и положи в папку {data_dir}/")
            sys.exit(1)

    print("Загружаю свечи...")
    c15m = load_csv(files["15m"])
    c1h  = load_csv(files["1h"])
    c4h  = load_csv(files["4h"])
    print(f"  15m: {len(c15m)} свечей")
    print(f"  1h:  {len(c1h)} свечей")
    print(f"  4h:  {len(c4h)} свечей")

    print("Запускаю бэктест...")
    trades, equity_curve = run_backtest(c15m, c1h, c4h, Decimal(str(args.equity)))
    print_report(trades, equity_curve, Decimal(str(args.equity)))


if __name__ == "__main__":
    main()
