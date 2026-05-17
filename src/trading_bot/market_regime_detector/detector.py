from __future__ import annotations

from decimal import Decimal

from trading_bot.config import StrategyConfig
from trading_bot.models import Candle, MarketRegime, RegimeSnapshot, to_decimal
from trading_bot.strategy_engine.indicators import atr, closes, ema


class MarketRegimeDetector:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def detect(self, candles_4h: list[Candle]) -> RegimeSnapshot:
        if len(candles_4h) < self.config.ema_slow + 5:
            return RegimeSnapshot(MarketRegime.UNKNOWN, Decimal("0"), Decimal("0"), Decimal("0"), "not enough 4h data")

        close_values = closes(candles_4h)
        ema_fast = ema(close_values, self.config.ema_fast)[-1]
        ema_mid = ema(close_values, self.config.ema_mid)[-1]
        ema_slow = ema(close_values, self.config.ema_slow)[-1]
        current_close = close_values[-1]
        atr_value = atr(candles_4h, self.config.atr_period)[-1]

        atr_pct = to_decimal((atr_value / current_close) * 100) if current_close else Decimal("0")
        trend_strength = to_decimal(abs(ema_fast - ema_slow) / current_close * 100) if current_close else Decimal("0")
        momentum_pct = to_decimal((current_close - close_values[-6]) / close_values[-6] * 100)

        if atr_pct > self.config.max_atr_pct:
            return RegimeSnapshot(MarketRegime.HIGH_VOLATILITY, atr_pct, trend_strength, momentum_pct, "atr too high")
        if atr_pct < self.config.min_atr_pct:
            return RegimeSnapshot(MarketRegime.LOW_VOLATILITY, atr_pct, trend_strength, momentum_pct, "atr too low")
        if abs(momentum_pct) > Decimal("4") and trend_strength > Decimal("1"):
            return RegimeSnapshot(MarketRegime.MOMENTUM, atr_pct, trend_strength, momentum_pct, "fast displacement")
        if current_close > ema_slow and ema_fast > ema_mid > ema_slow:
            return RegimeSnapshot(MarketRegime.TREND_UP, atr_pct, trend_strength, momentum_pct, "ema stack bullish")
        if current_close < ema_slow and ema_fast < ema_mid < ema_slow:
            return RegimeSnapshot(MarketRegime.TREND_DOWN, atr_pct, trend_strength, momentum_pct, "ema stack bearish")
        return RegimeSnapshot(MarketRegime.RANGE, atr_pct, trend_strength, momentum_pct, "mixed ema structure")

