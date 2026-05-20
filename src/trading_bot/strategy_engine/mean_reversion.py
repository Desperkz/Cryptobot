from __future__ import annotations

import logging
from decimal import Decimal

from trading_bot.config import StrategyConfig
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import Candle, Direction, EdgeSnapshot, MarketMetrics, MarketRegime, Signal, TradingStyle, to_decimal
from trading_bot.strategy_engine.edge import EdgeAnalyzer
from trading_bot.strategy_engine.indicators import atr, closes, ema, rsi, volumes, rolling_average


logger = logging.getLogger(__name__)


def _fmt_decimal(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{value:.{places}f}"
    except Exception:
        return str(value)


def _rsi_divergence(candles: list[Candle], direction: Direction, rsi_period: int, lookback: int = 20) -> bool:
    """
    Проверяет дивергенцию RSI:
    - SHORT: цена обновила максимум, RSI — нет (медвежья дивергенция)
    - LONG:  цена обновила минимум, RSI — нет (бычья дивергенция)

    Дивергенция значительно повышает надёжность сигнала.
    """
    if len(candles) < lookback + rsi_period + 5:
        return False

    vals = closes(candles)
    rsi_vals = rsi(vals, rsi_period)
    window_start = len(candles) - lookback

    if direction == Direction.SHORT:
        # Ищем два последних ценовых максимума
        highs = [c.high for c in candles[-lookback:]]
        curr_high = highs[-1]
        prev_high_idx = max(range(len(highs) - 5), key=lambda i: highs[i], default=0)
        prev_high = highs[prev_high_idx]

        curr_rsi = rsi_vals[-1]
        prev_rsi_idx = window_start + prev_high_idx
        if prev_rsi_idx < 0 or prev_rsi_idx >= len(rsi_vals):
            return False
        prev_rsi = rsi_vals[prev_rsi_idx]

        # Цена выше, RSI ниже — медвежья дивергенция
        return curr_high >= prev_high * Decimal("0.998") and curr_rsi < prev_rsi - 2

    elif direction == Direction.LONG:
        # Ищем два последних ценовых минимума
        lows = [c.low for c in candles[-lookback:]]
        curr_low = lows[-1]
        prev_low_idx = min(range(len(lows) - 5), key=lambda i: lows[i], default=0)
        prev_low = lows[prev_low_idx]

        curr_rsi = rsi_vals[-1]
        prev_rsi_idx = window_start + prev_low_idx
        if prev_rsi_idx < 0 or prev_rsi_idx >= len(rsi_vals):
            return False
        prev_rsi = rsi_vals[prev_rsi_idx]

        # Цена ниже, RSI выше — бычья дивергенция
        return curr_low <= prev_low * Decimal("1.002") and curr_rsi > prev_rsi + 2

    return False


def _entry_candle_ok(candles: list[Candle], direction: Direction, atr_val: Decimal) -> bool:
    """
    Фильтр свечи входа: не входить если последняя свеча — сильный импульс
    в направлении тренда (против нашей позиции).

    Лучше дождаться первой разворотной свечи.
    """
    if len(candles) < 3:
        return True

    last = candles[-1]
    body = abs(last.close - last.open)
    wick_top = last.high - max(last.close, last.open)
    wick_bottom = min(last.close, last.open) - last.low

    # Импульсная свеча — тело > 60% ATR
    if body > atr_val * Decimal("0.6"):
        if direction == Direction.SHORT and last.close > last.open:
            # Сильная бычья свеча — не входим в шорт прямо сейчас
            return False
        if direction == Direction.LONG and last.close < last.open:
            # Сильная медвежья свеча — не входим в лонг прямо сейчас
            return False

    # Хорошие разворотные паттерны — молот/звезда
    # Молот для лонга: длинный нижний хвост, маленькое тело
    if direction == Direction.LONG and wick_bottom > body * Decimal("2"):
        return True  # Молот — отличный сигнал для лонга

    # Падающая звезда для шорта: длинный верхний хвост, маленькое тело
    if direction == Direction.SHORT and wick_top > body * Decimal("2"):
        return True  # Падающая звезда — отличный сигнал для шорта

    return True


def _volume_confirms(candles: list[Candle], direction: Direction, lookback: int = 5) -> bool:
    """
    Объём на последних свечах должен расти при приближении к экстремуму.
    Это подтверждает что в зоне перекупленности/перепроданности активно торгуют.
    """
    if len(candles) < lookback + 1:
        return True

    recent_vols = [c.volume for c in candles[-lookback:]]
    prev_vols = [c.volume for c in candles[-lookback * 2:-lookback]]

    if not prev_vols:
        return True

    avg_recent = sum(recent_vols) / len(recent_vols)
    avg_prev = sum(prev_vols) / len(prev_vols)

    return avg_recent >= avg_prev * Decimal("0.9")  # Объём не падает


def _volume_confirmation(
    candles: list[Candle],
    min_ratio: Decimal,
    lookback: int = 5,
) -> tuple[bool, Decimal]:
    if len(candles) < lookback * 2:
        return False, Decimal("0")

    recent_vols = [c.volume for c in candles[-lookback:]]
    prev_vols = [c.volume for c in candles[-lookback * 2 : -lookback]]
    if not recent_vols or not prev_vols:
        return False, Decimal("0")

    avg_recent = sum(recent_vols) / len(recent_vols)
    avg_prev = sum(prev_vols) / len(prev_vols)
    if avg_prev <= 0:
        return False, Decimal("0")

    ratio = avg_recent / avg_prev
    return ratio >= min_ratio, ratio


def _reversal_candle_confirms(
    candles: list[Candle],
    direction: Direction,
    wick_body_ratio: Decimal = Decimal("1.8"),
) -> bool:
    if not candles:
        return False

    last = candles[-1]
    body = max(abs(last.close - last.open), last.close * Decimal("0.0001"))
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low

    if direction == Direction.LONG:
        return lower_wick / body >= wick_body_ratio and last.close > last.open
    if direction == Direction.SHORT:
        return upper_wick / body >= wick_body_ratio and last.close < last.open
    return False


def _edge_confirms(
    edge_snapshot: EdgeSnapshot | None,
    direction: Direction,
    min_score: Decimal,
) -> tuple[bool, tuple[str, ...]]:
    if edge_snapshot is None:
        return False, ()

    reasons: list[str] = []
    if edge_snapshot.liquidity_sweep and edge_snapshot.sweep_direction == direction:
        reasons.append("liquidity_sweep")
    if edge_snapshot.absorption and edge_snapshot.absorption_direction == direction:
        reasons.append("absorption")
    if edge_snapshot.structure_break and edge_snapshot.structure_direction == direction:
        reasons.append("structure_break")

    if not reasons:
        return False, ()
    if edge_snapshot.score < min_score and len(reasons) < 2:
        return False, tuple(reasons)
    return True, tuple(reasons)


class MeanReversionStrategy:
    """Counter-trend strategy for range/exhaustion regimes.

    Entry idea: price deviates from 4h EMA200 by more than N*ATR and RSI shows
    overbought/oversold pressure. Enhanced with RSI divergence detection,
    entry candle filter, and volume confirmation.
    """

    def __init__(
        self,
        config: StrategyConfig,
        regime_detector: MarketRegimeDetector,
        edge_analyzer: EdgeAnalyzer | None = None,
    ) -> None:
        self.config = config
        self.regime_detector = regime_detector
        self.edge_analyzer = edge_analyzer

    def generate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> Signal | None:
        def reject(
            reason: str,
            *,
            regime_value: str | None = None,
            deviation_atr_value: Decimal | None = None,
            rsi_1h_value: Decimal | None = None,
            rsi_15m_value: Decimal | None = None,
            confluence_value: int | None = None,
            volume_ratio_value: Decimal | None = None,
            edge_ok_value: bool | None = None,
            reversal_ok_value: bool | None = None,
        ) -> None:
            logger.info(
                "MR %s: signal=False reason=%s regime=%s deviation_atr=%s rsi_1h=%s rsi_15m=%s "
                "confluence=%s vol_ratio=%s edge=%s reversal=%s",
                symbol,
                reason,
                regime_value or "-",
                _fmt_decimal(deviation_atr_value),
                _fmt_decimal(rsi_1h_value),
                _fmt_decimal(rsi_15m_value),
                confluence_value if confluence_value is not None else "-",
                _fmt_decimal(volume_ratio_value),
                "-" if edge_ok_value is None else edge_ok_value,
                "-" if reversal_ok_value is None else reversal_ok_value,
            )

        min_required = max(self.config.ema_slow, self.config.atr_period, self.config.rsi_period) + 5
        if len(candles_15m) < min_required or len(candles_1h) < min_required or len(candles_4h) < min_required:
            reject("insufficient_candles")
            return None

        regime = self.regime_detector.detect(candles_4h)
        close_4h = closes(candles_4h)
        ema200_4h = to_decimal(ema(close_4h, self.config.ema_slow)[-1])
        atr_4h = to_decimal(atr(candles_4h, self.config.atr_period)[-1])
        price_4h = candles_4h[-1].close
        if atr_4h <= 0:
            reject("invalid_4h_atr", regime_value=regime.regime.value)
            return None

        deviation_atr = (price_4h - ema200_4h) / atr_4h

        # MR работает только в RANGE и EXHAUSTION режимах
        # В тренде и моментуме контртрендовые входы систематически проигрывают
        deviation_threshold = self.config.mean_reversion_deviation_atr
        if regime.regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN, MarketRegime.MOMENTUM}:
            reject(
                "blocked_regime",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
            )
            return None

        # RSI на 1h (более надёжный чем 15m)
        values_1h = closes(candles_1h)
        rsi_1h = to_decimal(rsi(values_1h, self.config.rsi_period)[-1])

        # RSI на 15m для дивергенции
        values_15m = closes(candles_15m)
        rsi_15m = to_decimal(rsi(values_15m, self.config.rsi_period)[-1])

        entry = candles_15m[-1].close
        atr_15m = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        if atr_15m <= 0:
            reject(
                "invalid_15m_atr",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
            )
            return None

        # Определяем направление по 1h RSI
        direction = Direction.NONE
        if deviation_atr <= -deviation_threshold and rsi_1h <= self.config.mean_reversion_rsi_oversold:
            direction = Direction.LONG
        elif deviation_atr >= deviation_threshold and rsi_1h >= self.config.mean_reversion_rsi_overbought:
            direction = Direction.SHORT
        if direction == Direction.NONE:
            reject(
                "no_extreme",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
            )
            return None

        # Edge analyzer
        edge_snapshot = self.edge_analyzer.analyze(candles_15m, direction, metrics) if self.edge_analyzer else None
        # Фильтр свечи входа
        if not _entry_candle_ok(candles_15m, direction, atr_15m):
            reject(
                "entry_candle",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
            )
            return None

        # Подтверждение объёмом
        vol_ok, volume_confirmation_ratio = _volume_confirmation(
            candles_15m,
            self.config.mean_reversion_min_volume_ratio,
        )

        # Дивергенция RSI (на 1h — более значимая)
        has_divergence = _rsi_divergence(candles_1h, direction, self.config.rsi_period)

        reversal_candle_ok = _reversal_candle_confirms(candles_15m, direction)
        edge_ok, edge_confirmation_reasons = _edge_confirms(
            edge_snapshot,
            direction,
            self.config.mean_reversion_min_edge_score,
        )
        rsi_15m_extreme = (
            (direction == Direction.SHORT and rsi_15m >= self.config.mean_reversion_rsi_overbought)
            or (direction == Direction.LONG and rsi_15m <= self.config.mean_reversion_rsi_oversold)
        )
        strong_deviation = abs(deviation_atr) >= deviation_threshold + Decimal("0.5")
        confirmation_flags = [
            "atr_deviation",
            "rsi_1h_extreme",
        ]
        if rsi_15m_extreme:
            confirmation_flags.append("rsi_15m_extreme")
        if strong_deviation:
            confirmation_flags.append("strong_deviation")
        if has_divergence:
            confirmation_flags.append("divergence")
        if vol_ok:
            confirmation_flags.append("volume")
        if reversal_candle_ok:
            confirmation_flags.append("reversal_candle")
        if edge_ok:
            confirmation_flags.append("edge")
        confluence = len(confirmation_flags)

        if self.config.mean_reversion_require_divergence and not has_divergence:
            reject(
                "missing_divergence",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
                confluence_value=confluence,
                volume_ratio_value=volume_confirmation_ratio,
                edge_ok_value=edge_ok,
                reversal_ok_value=reversal_candle_ok,
            )
            return None
        if not vol_ok:
            reject(
                "volume",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
                confluence_value=confluence,
                volume_ratio_value=volume_confirmation_ratio,
                edge_ok_value=edge_ok,
                reversal_ok_value=reversal_candle_ok,
            )
            return None
        if self.config.mean_reversion_require_edge_confirmation and not (edge_ok or reversal_candle_ok):
            reject(
                "missing_edge_or_reversal",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
                confluence_value=confluence,
                volume_ratio_value=volume_confirmation_ratio,
                edge_ok_value=edge_ok,
                reversal_ok_value=reversal_candle_ok,
            )
            return None
        if confluence < self.config.mean_reversion_min_confluence:
            reject(
                "low_confluence",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
                confluence_value=confluence,
                volume_ratio_value=volume_confirmation_ratio,
                edge_ok_value=edge_ok,
                reversal_ok_value=reversal_candle_ok,
            )
            return None

        # Расчёт позиции
        stop_distance = atr_15m * self.config.mean_reversion_stop_atr_multiplier
        rr = self.config.mean_reversion_take_profit_rr
        if direction == Direction.LONG:
            stop_loss = entry - stop_distance
            take_profit = entry + (stop_distance * rr)
        else:
            stop_loss = entry + stop_distance
            take_profit = entry - (stop_distance * rr)
        if stop_loss <= 0 or take_profit <= 0:
            reject(
                "invalid_prices",
                regime_value=regime.regime.value,
                deviation_atr_value=deviation_atr,
                rsi_1h_value=rsi_1h,
                rsi_15m_value=rsi_15m,
                confluence_value=confluence,
                volume_ratio_value=volume_confirmation_ratio,
                edge_ok_value=edge_ok,
                reversal_ok_value=reversal_candle_ok,
            )
            return None

        # Адаптивный confidence score
        confidence = Decimal("0.50")

        # Базовые бонусы
        if abs(deviation_atr) >= deviation_threshold + Decimal("0.5"):
            confidence += Decimal("0.08")  # Сильное отклонение
        if abs(deviation_atr) >= deviation_threshold + Decimal("1.5"):
            confidence += Decimal("0.06")  # Очень сильное отклонение

        # Дивергенция RSI — самый сильный сигнал
        if has_divergence:
            confidence += Decimal("0.15")

        # Подтверждение объёмом
        if vol_ok:
            confidence += Decimal("0.05")

        # Edge analyzer
        if edge_snapshot:
            confidence += edge_snapshot.score * Decimal("0.15")
        if edge_ok:
            confidence += Decimal("0.07")
        if reversal_candle_ok:
            confidence += Decimal("0.04")

        # Режим рынка — в Range лучше работает MR
        if regime.regime == MarketRegime.RANGE:
            confidence += Decimal("0.05")

        # RSI на 15m тоже в экстремальной зоне — двойное подтверждение
        if direction == Direction.SHORT and rsi_15m >= self.config.mean_reversion_rsi_overbought:
            confidence += Decimal("0.05")
        elif direction == Direction.LONG and rsi_15m <= self.config.mean_reversion_rsi_oversold:
            confidence += Decimal("0.05")

        volume_values = volumes(candles_15m)
        avg_volume = rolling_average(volume_values, self.config.volume_lookback)[-1]
        # Используем [-2] — последний закрытый бар, [-1] частично открыт и даёт заниженный объём
        volume_ratio = to_decimal(volume_values[-2] / avg_volume) if avg_volume > 0 else Decimal("0")
        atr_pct = atr_15m / entry * Decimal("100")

        divergence_str = "yes" if has_divergence else "no"
        edge_str = "yes" if edge_ok else "no"
        final_confidence = min(confidence, Decimal("0.90"))
        logger.info(
            "MR %s: signal=True direction=%s regime=%s deviation_atr=%s rsi_1h=%s rsi_15m=%s "
            "confluence=%s vol_ratio=%s edge=%s reversal=%s confidence=%s",
            symbol,
            direction.value,
            regime.regime.value,
            _fmt_decimal(deviation_atr),
            _fmt_decimal(rsi_1h),
            _fmt_decimal(rsi_15m),
            confluence,
            _fmt_decimal(volume_confirmation_ratio),
            edge_ok,
            reversal_candle_ok,
            _fmt_decimal(final_confidence, 4),
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            style=TradingStyle.INTRADAY,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=final_confidence,
            reason=(
                f"MEAN_REVERSION: deviation_atr={deviation_atr:.2f}, "
                f"rsi_1h={rsi_1h:.2f}, divergence={divergence_str}, "
                f"edge={edge_str}, confluence={confluence}, "
                f"regime={regime.regime.value}"
            ),
            metadata={
                "strategy": "MEAN_REVERSION",
                "regime": regime.regime.value,
                "deviation_atr": str(deviation_atr),
                "rsi": str(rsi_1h),
                "rsi_15m": str(rsi_15m),
                "divergence": divergence_str,
                "volume_ok": str(vol_ok),
                "volume_confirmation_ratio": str(volume_confirmation_ratio),
                "atr_pct": str(atr_pct),
                "volume_ratio": str(volume_ratio),
                "reversal_candle": str(reversal_candle_ok),
                "edge_confirms": str(edge_ok),
                "edge_confirmation_reasons": list(edge_confirmation_reasons),
                "mr_confluence": str(confluence),
                "mr_confirmation_flags": list(confirmation_flags),
                "spread_bps": str(metrics.spread_bps),
                "hour_utc": str((candles_15m[-1].close_time // 3_600_000) % 24),
                "edge_score": str(edge_snapshot.score) if edge_snapshot else "0",
                "edge_reasons": list(edge_snapshot.reasons) if edge_snapshot else [],
                "aggressive_delta": str(metrics.aggressive_buy_sell_delta),
                "open_interest_change_pct": str(metrics.open_interest_change_pct)
                if metrics.open_interest_change_pct is not None
                else None,
            },
        )
