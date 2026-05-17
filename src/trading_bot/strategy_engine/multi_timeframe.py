from __future__ import annotations

from decimal import Decimal

from trading_bot.config import EdgeFilterConfig, StrategyConfig
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import Candle, Direction, MarketMetrics, MarketRegime, Signal, TradingStyle, to_decimal
from trading_bot.strategy_engine.edge import EdgeAnalyzer
from trading_bot.strategy_engine.indicators import (
    atr,
    closes,
    ema,
    higher_high_higher_low,
    lower_high_lower_low,
    rolling_average,
    rsi,
    volumes,
)
from trading_bot.style_selector import StyleSelector


class MultiTimeframeStrategy:
    def __init__(
        self,
        config: StrategyConfig,
        regime_detector: MarketRegimeDetector,
        style_selector: StyleSelector,
        edge_filters: EdgeFilterConfig | None = None,
    ) -> None:
        self.config = config
        self.regime_detector = regime_detector
        self.style_selector = style_selector
        self.edge_filters = edge_filters
        self.edge_analyzer = EdgeAnalyzer(edge_filters) if edge_filters else None

    def generate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> Signal | None:
        min_required = max(self.config.ema_slow, self.config.volume_lookback, self.config.atr_period) + 5
        if len(candles_15m) < min_required or len(candles_1h) < min_required or len(candles_4h) < min_required:
            return None

        regime = self.regime_detector.detect(candles_4h)
        values_15m = closes(candles_15m)
        volume_values = volumes(candles_15m)
        avg_volume = rolling_average(volume_values, self.config.volume_lookback)[-1]
        volume_ratio = to_decimal(volume_values[-1] / avg_volume) if avg_volume > 0 else Decimal("0")
        style = self.style_selector.select(metrics, regime, volume_ratio)
        if style == TradingStyle.NO_TRADE:
            return None

        if self.config.use_funding_filter and metrics.funding_rate is not None:
            if abs(metrics.funding_rate) > self.config.max_abs_funding_rate:
                return None

        direction = self._direction(candles_1h, candles_4h, regime.regime)
        if direction == Direction.NONE:
            return None

        edge_snapshot = self.edge_analyzer.analyze(candles_15m, direction, metrics) if self.edge_analyzer else None
        if self.edge_filters and self.edge_filters.enabled:
            if not edge_snapshot or edge_snapshot.score < Decimal("0.60"):
                return None

        if not self._edge_confirmed(direction, metrics):
            return None

        if volume_ratio < max(self.config.min_volume_ratio, Decimal("1.70")):
            return None

        if not self._entry_confirmed(direction, candles_15m, volume_ratio):
            return None

        entry = candles_15m[-1].close
        atr_value = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        if entry <= 0:
            return None
        atr_pct = atr_value / entry * Decimal("100")
        if atr_pct < Decimal("0.35"):
            return None
        if atr_pct < self.config.min_atr_pct or atr_pct > self.config.max_atr_pct:
            return None

        stop_mult = self.config.stop_atr_multiplier[style.value]
        rr = self.config.take_profit_rr[style.value]
        stop_distance = atr_value * stop_mult
        if stop_distance <= 0:
            return None

        if direction == Direction.LONG:
            stop_loss = entry - stop_distance
            take_profit = entry + (stop_distance * rr)
        else:
            stop_loss = entry + stop_distance
            take_profit = entry - (stop_distance * rr)

        if stop_loss <= 0 or take_profit <= 0:
            return None

        confidence = Decimal("0.5")
        if regime.regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
            confidence += Decimal("0.15")
        if volume_ratio >= self.config.min_volume_ratio:
            confidence += Decimal("0.15")
        if metrics.spread_bps <= Decimal("4"):
            confidence += Decimal("0.10")
        if edge_snapshot:
            confidence += edge_snapshot.score * Decimal("0.20")

        rsi_value = to_decimal(rsi(values_15m, self.config.rsi_period)[-1])

        return Signal(
            symbol=symbol,
            direction=direction,
            style=style,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=min(confidence, Decimal("0.95")),
            reason=(
                f"TREND_FOLLOWING {style.value}: 4h={regime.regime.value}, volume_ratio={volume_ratio:.2f}, "
                f"atr_pct={atr_pct:.2f}, taker_buy_ratio={metrics.taker_buy_ratio}, "
                f"book_imbalance={metrics.order_book_imbalance:.3f}, "
                f"edge={edge_snapshot.score if edge_snapshot else 'n/a'}"
            ),
            metadata={
                "strategy": "TREND_FOLLOWING",
                "regime": regime.regime.value,
                "volume_ratio": str(volume_ratio),
                "spread_bps": str(metrics.spread_bps),
                "atr_pct": str(atr_pct),
                "rsi": str(rsi_value),
                "hour_utc": str((candles_15m[-1].close_time // 3_600_000) % 24),
                "edge_score": str(edge_snapshot.score) if edge_snapshot else "0",
                "edge_reasons": list(edge_snapshot.reasons) if edge_snapshot else [],
                "liquidity_sweep": bool(edge_snapshot and edge_snapshot.liquidity_sweep),
                "absorption": bool(edge_snapshot and edge_snapshot.absorption),
                "structure_break": bool(edge_snapshot and edge_snapshot.structure_break),
                "aggressive_delta": str(metrics.aggressive_buy_sell_delta),
                "open_interest_change_pct": str(metrics.open_interest_change_pct)
                if metrics.open_interest_change_pct is not None
                else None,
            },
        )

    def _edge_confirmed(self, direction: Direction, metrics: MarketMetrics) -> bool:
        if not self.edge_filters or not self.edge_filters.enabled:
            return True
        if direction == Direction.LONG:
            if metrics.order_book_imbalance < self.edge_filters.order_book_imbalance_min:
                return False
            if metrics.taker_buy_ratio is not None and metrics.taker_buy_ratio < self.edge_filters.taker_buy_ratio_long_min:
                return False
        if direction == Direction.SHORT:
            if metrics.order_book_imbalance > -self.edge_filters.order_book_imbalance_min:
                return False
            if metrics.taker_buy_ratio is not None and metrics.taker_buy_ratio > self.edge_filters.taker_buy_ratio_short_max:
                return False
        if metrics.open_interest_change_pct is not None:
            if metrics.open_interest_change_pct < self.edge_filters.open_interest_change_min_pct:
                return False
        return True

    def _direction(self, candles_1h: list[Candle], candles_4h: list[Candle], regime: MarketRegime) -> Direction:
        closes_1h = closes(candles_1h)
        closes_4h = closes(candles_4h)
        ema_fast_1h = ema(closes_1h, self.config.ema_fast)[-1]
        ema_mid_1h = ema(closes_1h, self.config.ema_mid)[-1]
        ema_slow_4h = ema(closes_4h, self.config.ema_slow)[-1]
        close_4h = closes_4h[-1]

        bullish = regime in {MarketRegime.TREND_UP, MarketRegime.MOMENTUM} and close_4h > ema_slow_4h
        bearish = regime in {MarketRegime.TREND_DOWN, MarketRegime.MOMENTUM} and close_4h < ema_slow_4h
        if bullish and ema_fast_1h > ema_mid_1h and higher_high_higher_low(candles_1h):
            return Direction.LONG
        if bearish and ema_fast_1h < ema_mid_1h and lower_high_lower_low(candles_1h):
            return Direction.SHORT
        return Direction.NONE

    def _entry_confirmed(self, direction: Direction, candles_15m: list[Candle], volume_ratio: Decimal) -> bool:
        values = closes(candles_15m)
        ema_fast = ema(values, self.config.ema_fast)[-1]
        ema_mid = ema(values, self.config.ema_mid)[-1]
        rsi_value = rsi(values, self.config.rsi_period)[-1]
        current = values[-1]

        if volume_ratio < self.config.min_volume_ratio:
            return False
        if direction == Direction.LONG:
            return current > ema_fast > ema_mid and 45 <= rsi_value <= 72
        if direction == Direction.SHORT:
            return current < ema_fast < ema_mid and 28 <= rsi_value <= 55
        return False
