from __future__ import annotations

from decimal import Decimal
from typing import Any

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
        signal, _diagnostic = self.evaluate(symbol, candles_15m, candles_1h, candles_4h, metrics)
        return signal

    def evaluate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> tuple[Signal | None, dict[str, Any]]:
        diagnostic = self._diagnostic(symbol)
        min_required = max(self.config.ema_slow, self.config.volume_lookback, self.config.atr_period) + 5
        if len(candles_15m) < min_required or len(candles_1h) < min_required or len(candles_4h) < min_required:
            diagnostic.update(
                {
                    "block_reason": "insufficient_candles",
                    "candles_15m": len(candles_15m),
                    "candles_1h": len(candles_1h),
                    "candles_4h": len(candles_4h),
                    "min_required": min_required,
                }
            )
            return None, diagnostic

        regime = self.regime_detector.detect(candles_4h)
        diagnostic["regime"] = regime.regime.value
        values_15m = closes(candles_15m)
        volume_values = volumes(candles_15m)
        avg_volume = rolling_average(volume_values, self.config.volume_lookback)[-1]
        volume_ratio = to_decimal(volume_values[-1] / avg_volume) if avg_volume > 0 else Decimal("0")
        diagnostic["volume_ratio"] = str(volume_ratio)
        style = self.style_selector.select(metrics, regime, volume_ratio)
        diagnostic["style"] = style.value
        if style == TradingStyle.NO_TRADE:
            diagnostic["block_reason"] = "no_trade_style"
            return None, diagnostic

        if self.config.use_funding_filter and metrics.funding_rate is not None:
            if abs(metrics.funding_rate) > self.config.max_abs_funding_rate:
                diagnostic.update(
                    {
                        "block_reason": "funding_extreme",
                        "funding_rate": str(metrics.funding_rate),
                        "max_abs_funding_rate": str(self.config.max_abs_funding_rate),
                    }
                )
                return None, diagnostic

        direction, direction_reason, direction_details = self._direction_with_diagnostic(
            candles_1h,
            candles_4h,
            regime.regime,
        )
        diagnostic["direction"] = direction.value
        diagnostic.update(direction_details)
        if direction == Direction.NONE:
            diagnostic["block_reason"] = direction_reason
            return None, diagnostic

        edge_snapshot = self.edge_analyzer.analyze(candles_15m, direction, metrics) if self.edge_analyzer else None
        if edge_snapshot:
            diagnostic.update(
                {
                    "edge_score": str(edge_snapshot.score),
                    "edge_reasons": list(edge_snapshot.reasons),
                    "liquidity_sweep": bool(edge_snapshot.liquidity_sweep),
                    "absorption": bool(edge_snapshot.absorption),
                    "structure_break": bool(edge_snapshot.structure_break),
                }
            )
        if self.edge_filters and self.edge_filters.enabled:
            if not edge_snapshot or edge_snapshot.score < Decimal("0.60"):
                diagnostic.update(
                    {
                        "block_reason": "edge_below_min",
                        "edge_min": "0.60",
                    }
                )
                return None, diagnostic

        if not self._edge_confirmed(direction, metrics):
            diagnostic.update(
                {
                    "block_reason": "order_flow_not_confirmed",
                    "taker_buy_ratio": str(metrics.taker_buy_ratio) if metrics.taker_buy_ratio is not None else None,
                    "book_imbalance": str(metrics.order_book_imbalance),
                    "open_interest_change_pct": str(metrics.open_interest_change_pct)
                    if metrics.open_interest_change_pct is not None
                    else None,
                }
            )
            return None, diagnostic

        min_volume_ratio = max(self.config.min_volume_ratio, Decimal("1.70"))
        if volume_ratio < min_volume_ratio:
            diagnostic.update({"block_reason": "weak_volume", "min_volume_ratio": str(min_volume_ratio)})
            return None, diagnostic

        if not self._entry_confirmed(direction, candles_15m, volume_ratio):
            diagnostic["block_reason"] = "entry_not_confirmed"
            return None, diagnostic

        entry = candles_15m[-1].close
        atr_value = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        if entry <= 0:
            diagnostic.update({"block_reason": "invalid_entry", "entry": str(entry)})
            return None, diagnostic
        atr_pct = atr_value / entry * Decimal("100")
        diagnostic.update({"entry": str(entry), "atr": str(atr_value), "atr_pct": str(atr_pct)})
        if atr_pct < Decimal("0.35"):
            diagnostic.update({"block_reason": "atr_too_low_for_trend_following", "min_trend_atr_pct": "0.35"})
            return None, diagnostic
        if atr_pct < self.config.min_atr_pct or atr_pct > self.config.max_atr_pct:
            diagnostic.update(
                {
                    "block_reason": "atr_out_of_range",
                    "min_atr_pct": str(self.config.min_atr_pct),
                    "max_atr_pct": str(self.config.max_atr_pct),
                }
            )
            return None, diagnostic

        stop_mult = self.config.stop_atr_multiplier[style.value]
        rr = self.config.take_profit_rr[style.value]
        stop_distance = atr_value * stop_mult
        if stop_distance <= 0:
            diagnostic.update({"block_reason": "invalid_stop_distance", "stop_distance": str(stop_distance)})
            return None, diagnostic

        if direction == Direction.LONG:
            stop_loss = entry - stop_distance
            take_profit = entry + (stop_distance * rr)
        else:
            stop_loss = entry + stop_distance
            take_profit = entry - (stop_distance * rr)

        if stop_loss <= 0 or take_profit <= 0:
            diagnostic.update(
                {
                    "block_reason": "invalid_exit_prices",
                    "stop_loss": str(stop_loss),
                    "take_profit": str(take_profit),
                }
            )
            return None, diagnostic

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
        diagnostic.update(
            {
                "decision": "SIGNAL",
                "block_reason": "passed",
                "confidence": str(min(confidence, Decimal("0.95"))),
                "rsi": str(rsi_value),
                "stop_loss": str(stop_loss),
                "take_profit": str(take_profit),
                "rr": str(rr),
            }
        )

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
        ), diagnostic

    def _diagnostic(self, symbol: str) -> dict[str, Any]:
        return {
            "strategy": "TREND_FOLLOWING",
            "symbol": symbol,
            "decision": "NO_SIGNAL",
            "block_reason": "not_evaluated",
            "direction": Direction.NONE.value,
        }

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
        direction, _reason, _details = self._direction_with_diagnostic(candles_1h, candles_4h, regime)
        return direction

    def _direction_with_diagnostic(
        self,
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        regime: MarketRegime,
    ) -> tuple[Direction, str, dict[str, Any]]:
        closes_1h = closes(candles_1h)
        closes_4h = closes(candles_4h)
        ema_fast_1h = to_decimal(ema(closes_1h, self.config.ema_fast)[-1])
        ema_mid_1h = to_decimal(ema(closes_1h, self.config.ema_mid)[-1])
        ema_slow_4h = to_decimal(ema(closes_4h, self.config.ema_slow)[-1])
        close_4h = to_decimal(closes_4h[-1])
        hh_hl = higher_high_higher_low(candles_1h)
        lh_ll = lower_high_lower_low(candles_1h)
        details: dict[str, Any] = {
            "trend_regime": regime.value,
            "close_4h": str(close_4h),
            "ema_slow_4h": str(ema_slow_4h),
            "ema_fast_1h": str(ema_fast_1h),
            "ema_mid_1h": str(ema_mid_1h),
            "hh_hl": bool(hh_hl),
            "lh_ll": bool(lh_ll),
        }

        if regime not in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN, MarketRegime.MOMENTUM}:
            return Direction.NONE, "no_trend_regime", details

        bullish_regime = regime in {MarketRegime.TREND_UP, MarketRegime.MOMENTUM}
        bearish_regime = regime in {MarketRegime.TREND_DOWN, MarketRegime.MOMENTUM}
        bullish_4h = bullish_regime and close_4h > ema_slow_4h
        bearish_4h = bearish_regime and close_4h < ema_slow_4h
        details.update(
            {
                "bullish_4h_alignment": bool(bullish_4h),
                "bearish_4h_alignment": bool(bearish_4h),
                "bullish_1h_ema_stack": bool(ema_fast_1h > ema_mid_1h),
                "bearish_1h_ema_stack": bool(ema_fast_1h < ema_mid_1h),
            }
        )

        if bullish_4h and ema_fast_1h > ema_mid_1h and hh_hl:
            return Direction.LONG, "passed", details
        if bearish_4h and ema_fast_1h < ema_mid_1h and lh_ll:
            return Direction.SHORT, "passed", details

        if bullish_regime and not bullish_4h:
            return Direction.NONE, "no_4h_bullish_alignment", details
        if bearish_regime and not bearish_4h:
            return Direction.NONE, "no_4h_bearish_alignment", details
        if bullish_4h and not ema_fast_1h > ema_mid_1h:
            return Direction.NONE, "no_1h_bullish_ema_stack", details
        if bearish_4h and not ema_fast_1h < ema_mid_1h:
            return Direction.NONE, "no_1h_bearish_ema_stack", details
        if bullish_4h and not hh_hl:
            return Direction.NONE, "no_1h_higher_high_higher_low", details
        if bearish_4h and not lh_ll:
            return Direction.NONE, "no_1h_lower_high_lower_low", details
        return Direction.NONE, "no_trend_structure", details

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
