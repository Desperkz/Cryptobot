from __future__ import annotations

from trading_bot.models import Candle


def closes(candles: list[Candle]) -> list[float]:
    return [float(c.close) for c in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [float(c.high) for c in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [float(c.low) for c in candles]


def volumes(candles: list[Candle]) -> list[float]:
    return [float(c.volume) for c in candles]


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * alpha) + (result[-1] * (1 - alpha)))
    return result


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < 2:
        return [50.0 for _ in values]
    gains = [0.0]
    losses = [0.0]
    for prev, current in zip(values, values[1:]):
        change = current - prev
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    result = [50.0 for _ in values]
    avg_gain = sum(gains[1 : period + 1]) / period if len(values) > period else 0.0
    avg_loss = sum(losses[1 : period + 1]) / period if len(values) > period else 0.0
    for index in range(period, len(values)):
        if index > period:
            avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
            avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
        if avg_loss == 0:
            result[index] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[index] = 100 - (100 / (1 + rs))
    return result


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    if not candles:
        return []
    true_ranges: list[float] = [float(candles[0].high - candles[0].low)]
    for previous, current in zip(candles, candles[1:]):
        high = float(current.high)
        low = float(current.low)
        prev_close = float(previous.close)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return ema(true_ranges, period)


def rolling_average(values: list[float], period: int) -> list[float]:
    result: list[float] = []
    for index in range(len(values)):
        start = max(0, index - period + 1)
        window = values[start : index + 1]
        result.append(sum(window) / len(window))
    return result


def higher_high_higher_low(candles: list[Candle], lookback: int = 5) -> bool:
    if len(candles) < lookback * 2:
        return False
    first = candles[-lookback * 2 : -lookback]
    second = candles[-lookback:]
    return max(c.high for c in second) > max(c.high for c in first) and min(c.low for c in second) > min(
        c.low for c in first
    )


def lower_high_lower_low(candles: list[Candle], lookback: int = 5) -> bool:
    if len(candles) < lookback * 2:
        return False
    first = candles[-lookback * 2 : -lookback]
    second = candles[-lookback:]
    return max(c.high for c in second) < max(c.high for c in first) and min(c.low for c in second) < min(
        c.low for c in first
    )

