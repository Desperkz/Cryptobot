"""
Бэктест Mean Reversion на нескольких монетах и двух таймфреймах (15m + 1h).

Использование:
    python backtest_multi.py --equity 250

CSV должны лежать в data/:
    data/BTCUSDT_15m.csv  data/BTCUSDT_1h.csv  data/BTCUSDT_4h.csv
    data/ETHUSDT_15m.csv  data/ETHUSDT_1h.csv  data/ETHUSDT_4h.csv
    data/SOLUSDT_15m.csv  ...
    data/BNBUSDT_15m.csv  ...
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


# ---------------------------------------------------------------------------
# Модели и утилиты
# ---------------------------------------------------------------------------

def to_dec(v) -> Decimal:
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


def load_csv(path: str) -> list[Candle]:
    candles = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            candles.append(Candle(
                open_time=int(row.get("open_time") or 0),
                open=to_dec(row["open"]),
                high=to_dec(row["high"]),
                low=to_dec(row["low"]),
                close=to_dec(row["close"]),
                volume=to_dec(row.get("volume", "0")),
                close_time=int(row.get("close_time") or row.get("open_time") or 0),
            ))
    return candles


# ---------------------------------------------------------------------------
# Индикаторы
# ---------------------------------------------------------------------------

def ema(values: list[Decimal], period: int) -> list[Decimal]:
    if not values or len(values) < period:
        return [values[-1]] * len(values) if values else []
    k = Decimal("2") / Decimal(period + 1)
    result = [sum(values[:period], Decimal("0")) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (Decimal("1") - k))
    pad = [result[0]] * (len(values) - len(result))
    return pad + result


def rsi(values: list[Decimal], period: int = 14) -> Decimal:
    if len(values) < period + 1:
        return Decimal("50")
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[-period - 1 + i] - values[-period - 1 + i - 1]
        (gains if diff > 0 else losses).append(abs(diff))
    ag = sum(gains, Decimal("0")) / period
    al = sum(losses, Decimal("0")) / period
    if al == 0:
        return Decimal("100")
    return Decimal("100") - Decimal("100") / (Decimal("1") + ag / al)


def atr(candles: list[Candle], period: int = 14) -> Decimal:
    if len(candles) < period + 1:
        return candles[-1].high - candles[-1].low if candles else Decimal("1")
    trs = []
    for i in range(1, period + 1):
        c = candles[-period - 1 + i]
        pc = candles[-period - 1 + i - 1].close
        trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return sum(trs, Decimal("0")) / period


def detect_regime(candles_4h: list[Candle]) -> str:
    closes = [c.close for c in candles_4h]
    if len(closes) < 50:
        return "RANGE"
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    p = closes[-1]
    if p > e20 > e50:
        return "TREND_UP"
    if p < e20 < e50:
        return "TREND_DOWN"
    return "RANGE"


def find_htf_index(ts: int, candles: list[Candle]) -> int:
    result = -1
    for i, c in enumerate(candles):
        if c.open_time <= ts:
            result = i
        else:
            break
    return result


# ---------------------------------------------------------------------------
# Сигнал Mean Reversion (универсальный для 15m и 1h)
# ---------------------------------------------------------------------------

AVOID_UTC = {0, 1, 2, 3, 4, 5, 6, 7}

RSI_OVERSOLD  = Decimal("25")
RSI_OVERBOUGHT = Decimal("75")
DEVIATION_ATR  = Decimal("2.0")
STOP_MULT      = Decimal("1.0")
RR             = Decimal("1.1")
VOL_MIN        = Decimal("1.2")


@dataclass
class Signal:
    direction: str
    tf: str         # "15m" или "1h"
    entry: Decimal
    stop: Decimal
    tp: Decimal


def mr_signal(
    candles_base: list[Candle],   # 15m или 1h
    candles_4h: list[Candle],
    tf_label: str,
) -> Signal | None:
    MIN = 210
    if len(candles_base) < MIN or len(candles_4h) < MIN:
        return None

    regime = detect_regime(candles_4h)
    closes_4h = [c.close for c in candles_4h]
    e200_4h = ema(closes_4h, 200)[-1]
    atr_4h = atr(candles_4h)
    price_4h = candles_4h[-1].close

    if atr_4h <= 0:
        return None

    deviation = (price_4h - e200_4h) / atr_4h
    threshold = DEVIATION_ATR
    if regime in ("TREND_UP", "TREND_DOWN"):
        threshold += Decimal("1.5")

    closes_base = [c.close for c in candles_base]
    rsi_val = rsi(closes_base)
    entry = candles_base[-1].close
    atr_base = atr(candles_base)

    if atr_base <= 0 or entry <= 0:
        return None

    # Фильтр волатильности
    atr_pct = atr_base / entry * 100
    if atr_pct < Decimal("0.15") or atr_pct > Decimal("5.0"):
        return None

    # Объём
    vols = [c.volume for c in candles_base[-21:-1]]
    avg_vol = sum(vols, Decimal("0")) / len(vols) if vols else Decimal("0")
    vol_ratio = candles_base[-1].volume / avg_vol if avg_vol > 0 else Decimal("1")
    if vol_ratio < VOL_MIN:
        return None

    direction = None
    if deviation <= -threshold and rsi_val <= RSI_OVERSOLD:
        direction = "LONG"
    elif deviation >= threshold and rsi_val >= RSI_OVERBOUGHT:
        direction = "SHORT"

    if not direction:
        return None

    stop_dist = atr_base * STOP_MULT
    if direction == "LONG":
        stop = entry - stop_dist
        tp = entry + stop_dist * RR
    else:
        stop = entry + stop_dist
        tp = entry - stop_dist * RR

    if stop <= 0 or tp <= 0:
        return None

    return Signal(direction, tf_label, entry, stop, tp)


# ---------------------------------------------------------------------------
# Риск
# ---------------------------------------------------------------------------

RISK_PCT    = Decimal("0.02")
MIN_RR      = Decimal("1.0")
FEE_SLIP    = Decimal("10") / Decimal("10000")   # ~10bps всё вместе


def calc_risk(sig: Signal, equity: Decimal):
    stop_dist   = abs(sig.entry - sig.stop)
    reward_dist = abs(sig.tp - sig.entry)
    if stop_dist <= 0 or reward_dist / stop_dist < MIN_RR:
        return None
    cost = stop_dist + sig.entry * FEE_SLIP
    budget = equity * RISK_PCT
    qty = budget / cost
    notional = qty * sig.entry
    if notional < Decimal("5"):
        return None
    return budget, qty * reward_dist  # risk_amount, reward_amount


# ---------------------------------------------------------------------------
# Бэктест одной монеты
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    tf: str
    direction: str
    pnl: Decimal
    r: Decimal
    equity_after: Decimal


def backtest_symbol(
    symbol: str,
    c15m: list[Candle],
    c1h:  list[Candle],
    c4h:  list[Candle],
    starting_equity: Decimal,
) -> tuple[list[Trade], list[float]]:

    equity = starting_equity
    equity_curve = [float(equity)]
    trades: list[Trade] = []
    peak = equity
    daily_start = equity
    last_day = -1
    losses_streak = 0
    cooldown_until = -1

    i = 220
    while i < len(c15m) - 80:
        candle = c15m[i]

        # UTC-фильтр
        hour = (candle.close_time // 3_600_000) % 24
        if hour in AVOID_UTC:
            i += 1
            continue

        # Кулдаун
        if candle.open_time < cooldown_until:
            i += 1
            continue

        # Дневной сброс
        day = candle.open_time // 86_400_000
        if day != last_day:
            daily_start = equity
            last_day = day

        # Дневной лимит
        if equity < daily_start * Decimal("0.94"):
            i += 1
            continue

        idx_4h = find_htf_index(candle.open_time, c4h)
        idx_1h = find_htf_index(candle.open_time, c1h)
        if idx_4h < 210 or idx_1h < 210:
            i += 1
            continue

        sl_4h  = c4h[max(0, idx_4h - 250): idx_4h + 1]
        sl_1h  = c1h[max(0, idx_1h - 250): idx_1h + 1]
        sl_15m = c15m[max(0, i - 250): i + 1]

        # Только 1h сигналы — они надёжнее (56% WR vs 49% на 15m)
        sig_15m = None
        sig_1h  = mr_signal(sl_1h,  sl_4h, "1h")

        # Выбираем: предпочитаем 1h (более надёжный), если оба есть и согласны
        signal = None
        if sig_15m and sig_1h:
            if sig_15m.direction == sig_1h.direction:
                signal = sig_1h   # оба согласны — берём 1h
            # Иначе конфликт — пропускаем
        elif sig_1h:
            signal = sig_1h
        elif sig_15m:
            signal = sig_15m

        if signal is None:
            i += 1
            continue

        pos = calc_risk(signal, equity)
        if pos is None:
            i += 1
            continue
        risk_amount, reward_amount = pos

        # Симуляция — ищем выход на 15m свечах (до 120 свечей = 30 часов)
        pnl = Decimal("0")
        exit_i = i + 1
        for j, future in enumerate(c15m[i + 1: min(i + 121, len(c15m))]):
            exit_i = i + 1 + j
            if signal.direction == "LONG":
                if future.low <= signal.stop:
                    pnl = -risk_amount
                    break
                if future.high >= signal.tp:
                    pnl = reward_amount
                    break
            else:
                if future.high >= signal.stop:
                    pnl = -risk_amount
                    break
                if future.low <= signal.tp:
                    pnl = reward_amount
                    break

        equity += pnl
        equity_curve.append(float(equity))
        peak = max(peak, equity)

        if pnl < 0:
            losses_streak += 1
            if losses_streak >= 2:
                cooldown_until = candle.open_time + 2 * 3_600_000
        else:
            losses_streak = 0

        r = pnl / risk_amount if risk_amount > 0 else Decimal("0")
        trades.append(Trade(symbol, signal.tf, signal.direction, pnl, r, equity))
        i = exit_i + 1

    return trades, equity_curve


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

def report(all_trades: list[Trade], curves: dict[str, list[float]], starting: Decimal) -> None:
    if not all_trades:
        print("\n❌ Сделок не найдено.")
        return

    wins   = [t for t in all_trades if t.pnl > 0]
    losses = [t for t in all_trades if t.pnl <= 0]
    gp = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in losses))
    pf = gp / gl if gl > 0 else gp
    wr = len(wins) / len(all_trades) * 100
    avg_r = sum(t.r for t in all_trades) / len(all_trades)

    symbols = sorted(set(t.symbol for t in all_trades))

    print("\n" + "=" * 58)
    print("  РЕЗУЛЬТАТЫ — MEAN REVERSION (15m + 1h) — MULTI SYMBOL")
    print("=" * 58)
    print(f"  Монет протестировано  : {len(symbols)}")
    print(f"  Всего сделок          : {len(all_trades)}")
    print(f"  Wins / Losses         : {len(wins)} / {len(losses)}")
    print(f"  Winrate               : {wr:.1f}%")
    print(f"  Profit Factor         : {pf:.2f}  {'✅' if pf >= 1.2 else '❌'}")
    print(f"  Avg R-multiple        : {avg_r:.3f}")
    print("-" * 58)

    # По монетам
    print(f"  {'Монета':<12} {'Сделок':>6} {'WR%':>6} {'PF':>6} {'1h сделок':>10}")
    print("  " + "-" * 44)
    for sym in symbols:
        st = [t for t in all_trades if t.symbol == sym]
        sw = [t for t in st if t.pnl > 0]
        sgp = sum(t.pnl for t in sw)
        sgl = abs(sum(t.pnl for t in st if t.pnl <= 0))
        spf = sgp / sgl if sgl > 0 else sgp
        swr = len(sw) / len(st) * 100 if st else 0
        s1h = len([t for t in st if t.tf == "1h"])
        print(f"  {sym:<12} {len(st):>6} {swr:>5.1f}% {spf:>6.2f} {s1h:>10}")

    print("-" * 58)
    # По таймфреймам
    t15 = [t for t in all_trades if t.tf == "15m"]
    t1h = [t for t in all_trades if t.tf == "1h"]
    def tf_wr(lst): return f"{len([t for t in lst if t.pnl>0])}/{len(lst)} ({len([t for t in lst if t.pnl>0])/len(lst)*100:.0f}%)" if lst else "—"
    print(f"  15m сигналы           : {tf_wr(t15)}")
    print(f"  1h  сигналы           : {tf_wr(t1h)}")

    print("=" * 58)

    if pf >= 1.2 and len(all_trades) >= 30:
        print("\n✅ Стратегия прошла критерии.")
        print("   Следующий шаг: paper trading 4–8 недель.\n")
    else:
        reasons = []
        if pf < 1.2:
            reasons.append(f"PF {pf:.2f} < 1.2")
        if len(all_trades) < 30:
            reasons.append(f"мало сделок ({len(all_trades)})")
        print(f"\n❌ Не прошла: {', '.join(reasons)}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SYMBOLS = ["BNBUSDT", "SOLUSDT"]  # BTC и ETH слабые на этой стратегии


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--equity", type=float, default=250)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    data = Path(args.data_dir)
    eq = Decimal(str(args.equity))

    all_trades: list[Trade] = []
    curves: dict[str, list[float]] = {}
    missing = []

    for sym in SYMBOLS:
        f15 = data / f"{sym}_15m.csv"
        f1h  = data / f"{sym}_1h.csv"
        f4h  = data / f"{sym}_4h.csv"
        if not (f15.exists() and f1h.exists() and f4h.exists()):
            missing.append(sym)
            continue
        print(f"  Загружаю {sym}...")
        c15 = load_csv(f15)
        c1h = load_csv(f1h)
        c4h = load_csv(f4h)
        trades, curve = backtest_symbol(sym, c15, c1h, c4h, eq)
        all_trades.extend(trades)
        curves[sym] = curve
        print(f"    → {len(trades)} сделок")

    if missing:
        print(f"\n  ⚠️  Нет данных для: {', '.join(missing)}")
        print(f"     Запусти download_candles.py чтобы скачать.\n")

    report(all_trades, curves, eq)


if __name__ == "__main__":
    main()
