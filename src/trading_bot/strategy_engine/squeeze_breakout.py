"""
Squeeze Breakout Strategy — стратегия пробоя после сжатия волатильности.

Логика:
1. Обнаруживает "squeeze" — состояние когда Bollinger Bands сжимаются внутрь
   Keltner Channel. Это признак накопления энергии перед сильным движением.
2. Ждёт пробоя с подтверждением объёма и направления.
3. Входит в направлении пробоя с целью поймать начало нового тренда.

Squeeze работает особенно хорошо:
- На монетах которые долго торговались в боковике
- Перед крупными движениями после консолидации
- Дополняет Mean Reversion (MR ловит коррекции, SQZ ловит начало трендов)
"""
from __future__ import annotations

import logging

from decimal import Decimal

logger = logging.getLogger("trading_bot.squeeze")

from trading_bot.config import StrategyConfig
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import (
    Candle, Direction, MarketMetrics, MarketRegime,
    Signal, TradingStyle, to_decimal
)
from trading_bot.strategy_engine.indicators import (
    atr, closes, ema, highs, lows, rsi, volumes
)


def _bollinger_bands(
    values: list[float],
    period: int = 20,
    multiplier: float = 2.0,
) -> tuple[list[float], list[float], list[float]]:
    """Возвращает (upper, middle, lower) полосы Боллинджера."""
    if len(values) < period:
        m = values[-1] if values else 0.0
        return [m] * len(values), [m] * len(values), [m] * len(values)

    middle = []
    upper = []
    lower = []

    for i in range(len(values)):
        if i < period - 1:
            middle.append(values[i])
            upper.append(values[i])
            lower.append(values[i])
            continue
        window = values[i - period + 1: i + 1]
        avg = sum(window) / period
        variance = sum((x - avg) ** 2 for x in window) / period
        std = variance ** 0.5
        middle.append(avg)
        upper.append(avg + multiplier * std)
        lower.append(avg - multiplier * std)

    return upper, middle, lower


def _keltner_channels(
    candles: list[Candle],
    period: int = 20,
    multiplier: float = 1.5,
) -> tuple[list[float], list[float], list[float]]:
    """Возвращает (upper, middle, lower) каналы Келтнера."""
    close_vals = closes(candles)
    atr_vals = atr(candles, period)

    ema_vals = ema(close_vals, period)
    upper = [e + multiplier * a for e, a in zip(ema_vals, atr_vals)]
    lower = [e - multiplier * a for e, a in zip(ema_vals, atr_vals)]
    return upper, ema_vals, lower


def _detect_squeeze(
    candles: list[Candle],
    bb_period: int = 20,
    kc_period: int = 20,
) -> tuple[bool, int]:
    """
    Обнаруживает squeeze — BB внутри KC.

    Возвращает:
    - is_squeeze: True если сейчас в squeeze
    - squeeze_bars: сколько баров подряд идёт squeeze
    """
    if len(candles) < bb_period + 5:
        return False, 0

    close_vals = closes(candles)
    bb_upper, bb_mid, bb_lower = _bollinger_bands(close_vals, bb_period)
    kc_upper, kc_mid, kc_lower = _keltner_channels(candles, kc_period)

    # Считаем сколько подряд баров BB внутри KC
    squeeze_bars = 0
    for i in range(len(candles) - 1, max(len(candles) - 30, 0), -1):
        if bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i]:
            squeeze_bars += 1
        else:
            break

    is_squeeze = squeeze_bars > 0
    return is_squeeze, squeeze_bars


def _squeeze_flags(candles: list[Candle], bb_period: int = 20, kc_period: int = 20) -> list[bool]:
    if len(candles) < bb_period + 5:
        return [False] * len(candles)
    close_vals = closes(candles)
    bb_upper, _, bb_lower = _bollinger_bands(close_vals, bb_period)
    kc_upper, _, kc_lower = _keltner_channels(candles, kc_period)
    return [bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i] for i in range(len(candles))]


def _recent_squeeze_release(
    candles: list[Candle],
    min_squeeze_bars: int,
    lookback: int = 3,
) -> tuple[bool, int, int]:
    """Return True when BB has just expanded out of KC after a valid squeeze."""
    flags = _squeeze_flags(candles)
    if len(flags) < min_squeeze_bars + 2:
        return False, 0, 0
    for release_offset in range(0, min(lookback, len(flags) - 1)):
        release_idx = len(flags) - 1 - release_offset
        if flags[release_idx]:
            continue
        squeeze_bars = 0
        i = release_idx - 1
        while i >= 0 and flags[i]:
            squeeze_bars += 1
            i -= 1
        if squeeze_bars >= min_squeeze_bars:
            return True, squeeze_bars, release_offset
    return False, 0, 0


def _confirmation_volume_ratio(
    candles: list[Candle],
    volume_lookback: int,
    confirmation_window: int,
) -> float:
    """Use the strongest volume in the release/follow-through window."""
    vol_vals = volumes(candles)
    if not vol_vals:
        return 1.0
    confirmation_window = max(1, min(confirmation_window, len(vol_vals)))
    baseline_end = len(vol_vals) - confirmation_window
    baseline_start = max(0, baseline_end - max(1, volume_lookback))
    baseline = vol_vals[baseline_start:baseline_end]
    if not baseline:
        baseline = vol_vals[: -confirmation_window] or vol_vals[-volume_lookback:]
    avg_vol = sum(baseline) / len(baseline) if baseline else 0
    if avg_vol <= 0:
        return 1.0
    return max(vol_vals[-confirmation_window:]) / avg_vol


def _compression_range(
    candles: list[Candle],
    squeeze_bars: int,
    release_offset: int,
) -> tuple[Decimal, Decimal] | None:
    if len(candles) < 3 or squeeze_bars <= 0:
        return None
    range_end = len(candles) - 1 - max(0, release_offset)
    if range_end <= 0:
        return None
    lookback = min(max(2, squeeze_bars), 30, range_end)
    window = candles[range_end - lookback : range_end]
    if not window:
        return None
    return max(candle.high for candle in window), min(candle.low for candle in window)


def _breakout_quality(
    candles: list[Candle],
    direction: Direction,
    atr_value: Decimal,
    squeeze_bars: int,
    release_offset: int,
) -> dict[str, Decimal] | None:
    if atr_value <= 0:
        return None
    compression = _compression_range(candles, squeeze_bars, release_offset)
    if compression is None:
        return None
    range_high, range_low = compression
    close = candles[-1].close
    breakout_distance = close - range_high if direction == Direction.LONG else range_low - close
    breakout_atr = breakout_distance / atr_value
    return {
        "range_high": range_high,
        "range_low": range_low,
        "breakout_distance": breakout_distance,
        "breakout_atr": breakout_atr,
    }


def _breakout_retest_quality(
    candles: list[Candle],
    direction: Direction,
    breakout_level: Decimal,
    atr_value: Decimal,
    release_offset: int,
    lookback_bars: int,
    tolerance_atr: Decimal,
    min_rejection_body_atr: Decimal,
) -> dict[str, object]:
    if atr_value <= 0 or lookback_bars <= 0 or direction not in {Direction.LONG, Direction.SHORT}:
        return {"confirmed": False}

    release_idx = len(candles) - 1 - max(0, release_offset)
    # Retest can only happen after the initial release candle.
    search_start = min(max(0, release_idx + 1), len(candles))
    if search_start >= len(candles):
        return {"confirmed": False}

    window = candles[max(search_start, len(candles) - lookback_bars) :]
    if not window:
        return {"confirmed": False}

    tolerance = atr_value * tolerance_atr
    min_body = atr_value * min_rejection_body_atr
    for bars_ago, candle in enumerate(reversed(window)):
        body = abs(candle.close - candle.open)
        lower_wick = min(candle.open, candle.close) - candle.low
        upper_wick = candle.high - max(candle.open, candle.close)
        if direction == Direction.LONG:
            touched = candle.low <= breakout_level + tolerance
            held_level = candle.close >= breakout_level
            rejection = candle.close > candle.open and body >= min_body
            absorption = lower_wick >= max(body, min_body) * Decimal("1.5")
        else:
            touched = candle.high >= breakout_level - tolerance
            held_level = candle.close <= breakout_level
            rejection = candle.close < candle.open and body >= min_body
            absorption = upper_wick >= max(body, min_body) * Decimal("1.5")

        if touched and held_level and (rejection or absorption):
            return {
                "confirmed": True,
                "bars_ago": bars_ago,
                "level": breakout_level,
                "tolerance_atr": tolerance_atr,
                "rejection_body_atr": body / atr_value,
                "type": "rejection" if rejection else "absorption",
            }

    return {
        "confirmed": False,
        "level": breakout_level,
        "tolerance_atr": tolerance_atr,
    }


def _squeeze_momentum(candles: list[Candle], period: int = 20) -> list[float]:
    """
    Momentum индикатор для squeeze (по John Carter).
    Показывает направление и силу накопленной энергии.
    """
    if len(candles) < period:
        return [0.0] * len(candles)

    close_vals = closes(candles)
    high_vals = highs(candles)
    low_vals = lows(candles)
    atr_vals = atr(candles, period)
    ema_vals = ema(close_vals, period)

    result = []
    for i in range(len(candles)):
        if i < period:
            result.append(0.0)
            continue

        # Midpoint высоких/низких за период
        highest_high = max(high_vals[i - period + 1: i + 1])
        lowest_low = min(low_vals[i - period + 1: i + 1])
        midpoint = (highest_high + lowest_low) / 2

        # Delta от средней цены и EMA
        delta = close_vals[i] - (midpoint + ema_vals[i]) / 2
        result.append(delta)

    # Линейная регрессия для сглаживания
    smoothed = ema(result, 5)
    return smoothed


def _breakout_direction(
    candles: list[Candle],
    momentum: list[float],
    volume_ratio: float,
    min_volume_ratio: float,
) -> Direction | None:
    """
    Определяет направление пробоя после squeeze.

    Условия входа в LONG:
    - Momentum был отрицательным и начинает расти (разворот снизу)
    - Последние 2 бара momentum растёт
    - Объём выше среднего
    - Последняя свеча бычья

    Аналогично для SHORT.
    """
    if len(momentum) < 5:
        return None

    m = momentum
    last = m[-1]
    prev = m[-2]
    prev2 = m[-3]

    last_candle = candles[-1]
    is_bull_candle = last_candle.close > last_candle.open
    is_bear_candle = last_candle.close < last_candle.open

    # LONG: momentum разворачивается вверх из отрицательной зоны
    # или уверенно растёт
    long_momentum = (
        (last > prev > prev2 and last > 0) or  # уверенный рост
        (prev < 0 and last > prev and last > -abs(prev) * 0.3)  # разворот
    )

    # SHORT: momentum разворачивается вниз из положительной зоны
    short_momentum = (
        (last < prev < prev2 and last < 0) or  # уверенное падение
        (prev > 0 and last < prev and last < abs(prev) * 0.3)  # разворот
    )

    if long_momentum and is_bull_candle and volume_ratio >= min_volume_ratio:
        return Direction.LONG
    if short_momentum and is_bear_candle and volume_ratio >= min_volume_ratio:
        return Direction.SHORT

    return None


class SqueezeBreakoutStrategy:
    """
    Стратегия пробоя после сжатия волатильности (Squeeze Breakout).

    Работает лучше всего:
    - В боковых рынках перед началом нового тренда
    - На монетах с высокой волатильностью (BNBUSDT, SOLUSDT, INJUSDT)
    - На 1h таймфрейме (основной сигнал)

    Дополняет Mean Reversion:
    - MR: входит на экстремумах против тренда
    - SQZ: входит на начале нового движения после боковика
    """

    def __init__(
        self,
        config: StrategyConfig,
        regime_detector: MarketRegimeDetector,
    ) -> None:
        self.config = config
        self.regime_detector = regime_detector

    def generate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> Signal | None:
        min_required = 50
        if len(candles_1h) < min_required or len(candles_4h) < min_required:
            return None

        regime = self.regime_detector.detect(candles_4h)

        # Squeeze лучше работает в Range режиме
        # В сильном тренде пробои ненадёжны
        if regime.regime == MarketRegime.TREND_UP or regime.regime == MarketRegime.TREND_DOWN:
            # В тренде требуем более сильный squeeze
            min_squeeze_bars = 8
        else:
            min_squeeze_bars = 4

        # Обнаруживаем squeeze на 1h и release после сжатия.
        is_squeeze, squeeze_bars = _detect_squeeze(candles_1h)
        is_release, release_bars, release_offset = _recent_squeeze_release(
            candles_1h,
            min_squeeze_bars,
            lookback=max(1, self.config.squeeze_release_lookback_bars),
        )
        if is_release:
            squeeze_bars = max(squeeze_bars, release_bars)
        squeeze_state = "release" if is_release else "build"
        logger.info(
            "SQZ %s: squeeze=%s release=%s bars=%d min=%d",
            symbol, is_squeeze, is_release, squeeze_bars, min_squeeze_bars,
        )

        if not is_release and (not is_squeeze or squeeze_bars < min_squeeze_bars):
            return None

        # Momentum для определения направления
        momentum = _squeeze_momentum(candles_1h)
        if not momentum:
            return None

        confirmation_window = release_offset + 1 if is_release else 2
        vol_ratio = _confirmation_volume_ratio(candles_1h, self.config.volume_lookback, confirmation_window)

        # Определяем направление пробоя
        direction = _breakout_direction(candles_1h, momentum, vol_ratio, float(self.config.min_volume_ratio))
        if direction is None:
            return None

        # ATR для расчёта стопа
        entry = candles_1h[-1].close
        atr_1h = to_decimal(atr(candles_1h, self.config.atr_period)[-1])

        if atr_1h <= 0 or entry <= 0:
            return None

        # Фильтр волатильности
        atr_pct = atr_1h / entry * Decimal("100")
        if atr_pct < Decimal("0.15") or atr_pct > Decimal("6.0"):
            return None

        quality = _breakout_quality(candles_1h, direction, atr_1h, squeeze_bars, release_offset if is_release else 0)
        if quality is None:
            return None
        breakout_atr = quality["breakout_atr"]
        min_breakout_atr = (
            self.config.squeeze_release_min_breakout_atr
            if is_release
            else self.config.squeeze_early_min_breakout_atr
        )
        if breakout_atr < min_breakout_atr:
            return None
        if breakout_atr > self.config.squeeze_max_extension_atr:
            return None
        if not is_release:
            if squeeze_bars < min_squeeze_bars + self.config.squeeze_early_min_bars_extra:
                return None
            if Decimal(str(vol_ratio)) < self.config.squeeze_early_min_volume_ratio:
                return None

        retest_level = quality["range_high"] if direction == Direction.LONG else quality["range_low"]
        retest = _breakout_retest_quality(
            candles_1h,
            direction,
            retest_level,
            atr_1h,
            release_offset if is_release else 0,
            self.config.squeeze_retest_lookback_bars,
            self.config.squeeze_retest_tolerance_atr,
            self.config.squeeze_retest_min_rejection_body_atr,
        )
        retest_confirmed = bool(retest.get("confirmed"))
        retest_required = bool(
            self.config.squeeze_retest_enabled
            and is_release
            and release_offset >= self.config.squeeze_retest_required_after_release_offset
        )
        if retest_required and not retest_confirmed:
            return None

        # Стоп и тейк-профит
        # Для breakout используем более широкий стоп (1.5 ATR)
        # и более агрессивный тейк (2.0 RR) — цена может далеко уйти
        stop_distance = atr_1h * Decimal("1.5")
        rr = Decimal("2.0")  # агрессивный RR для breakout

        if direction == Direction.LONG:
            stop_loss = entry - stop_distance
            take_profit = entry + stop_distance * rr
        else:
            stop_loss = entry + stop_distance
            take_profit = entry - stop_distance * rr

        if stop_loss <= 0 or take_profit <= 0:
            return None

        # Confidence score
        confidence = Decimal("0.50")

        # Больше баров в squeeze = сильнее потенциальный пробой
        if squeeze_bars >= 8:
            confidence += Decimal("0.10")
        if squeeze_bars >= 15:
            confidence += Decimal("0.08")

        # Объём подтверждает пробой
        if vol_ratio >= 1.5:
            confidence += Decimal("0.08")
        if vol_ratio >= 2.0:
            confidence += Decimal("0.07")

        if breakout_atr >= Decimal("0.25"):
            confidence += Decimal("0.04")
        if breakout_atr >= Decimal("0.75"):
            confidence += Decimal("0.04")

        # В Range режиме squeeze надёжнее
        if regime.regime == MarketRegime.RANGE:
            confidence += Decimal("0.07")
        if is_release:
            confidence += Decimal("0.06")
        if retest_confirmed:
            confidence += Decimal("0.05")

        # Сила momentum
        mom_strength = abs(momentum[-1])
        if mom_strength > 0:
            confidence += min(Decimal(str(mom_strength / 100)), Decimal("0.10"))

        # Подтверждение на 4h
        _, squeeze_4h = _detect_squeeze(candles_4h, bb_period=20, kc_period=20)
        if squeeze_4h >= 3:
            confidence += Decimal("0.05")  # squeeze на старшем TF = сильнее

        return Signal(
            symbol=symbol,
            direction=direction,
            style=TradingStyle.INTRADAY,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=min(confidence, Decimal("0.88")),
            reason=(
                f"SQUEEZE_BREAKOUT: bars={squeeze_bars}, "
                f"vol_ratio={vol_ratio:.2f}, "
                f"momentum={momentum[-1]:.3f}, "
                f"state={squeeze_state}, "
                f"breakout_atr={breakout_atr:.2f}, "
                f"retest={'yes' if retest_confirmed else 'no'}, "
                f"regime={regime.regime.value}"
            ),
            metadata={
                "strategy": "SQUEEZE_BREAKOUT",
                "regime": regime.regime.value,
                "squeeze_state": squeeze_state,
                "squeeze_entry_timing": "release_followthrough" if is_release else "early_breakout",
                "squeeze_bars": squeeze_bars,
                "squeeze_release_offset": release_offset if is_release else None,
                "squeeze_bars_4h": squeeze_4h,
                "momentum": str(round(momentum[-1], 4)),
                "volume_ratio": str(round(vol_ratio, 3)),
                "breakout_atr": str(breakout_atr),
                "compression_high": str(quality["range_high"]),
                "compression_low": str(quality["range_low"]),
                "squeeze_retest_required": retest_required,
                "squeeze_retest_confirmed": retest_confirmed,
                "squeeze_retest_bars_ago": retest.get("bars_ago"),
                "squeeze_retest_level": str(retest.get("level")) if retest.get("level") is not None else None,
                "squeeze_retest_type": retest.get("type"),
                "squeeze_retest_rejection_body_atr": str(retest.get("rejection_body_atr"))
                if retest.get("rejection_body_atr") is not None
                else None,
                "atr_pct": str(atr_pct),
                "rr": str(rr),
                "hour_utc": str((candles_1h[-1].close_time // 3_600_000) % 24),
            },
        )
