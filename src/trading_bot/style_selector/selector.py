from __future__ import annotations

from decimal import Decimal

from trading_bot.models import MarketMetrics, MarketRegime, RegimeSnapshot, TradingStyle


class StyleSelector:
    def __init__(self, max_spread_bps: Decimal, min_quote_volume: Decimal) -> None:
        self.max_spread_bps = max_spread_bps
        self.min_quote_volume = min_quote_volume

    def select(self, metrics: MarketMetrics, regime: RegimeSnapshot, volume_ratio_15m: Decimal) -> TradingStyle:
        if metrics.spread_bps > self.max_spread_bps or metrics.quote_volume_24h < self.min_quote_volume:
            return TradingStyle.NO_TRADE
        if regime.regime in {MarketRegime.HIGH_VOLATILITY, MarketRegime.LOW_VOLATILITY, MarketRegime.UNKNOWN}:
            return TradingStyle.NO_TRADE
        if (
            metrics.spread_bps <= self.max_spread_bps / Decimal("2")
            and volume_ratio_15m >= Decimal("1.5")
            and regime.regime == MarketRegime.MOMENTUM
        ):
            return TradingStyle.SCALPING
        if regime.regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN} and regime.trend_strength >= Decimal("2"):
            return TradingStyle.SWING
        if regime.regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN, MarketRegime.MOMENTUM}:
            return TradingStyle.INTRADAY
        return TradingStyle.NO_TRADE

