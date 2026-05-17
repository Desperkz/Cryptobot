from __future__ import annotations

from decimal import Decimal

from trading_bot.config import StrategyConfig
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import Candle, Direction, MarketMetrics, MarketRegime, Signal, TradingStyle, to_decimal
from trading_bot.strategy_engine.edge import EdgeAnalyzer
from trading_bot.strategy_engine.indicators import atr, closes, ema, rsi, volumes, rolling_average


def _volume_ratio(candles: list[Candle], lookback: int) -> Decimal:
    volume_values = volumes(candles)
    avg_volume = rolling_average(volume_values, lookback)[-1]
    return to_decimal(volume_values[-1] / avg_volume) if avg_volume > 0 else Decimal("0")


def _trend_direction(
    candles_4h: list[Candle],
    config: StrategyConfig,
    regime: MarketRegime,
) -> Direction:
    if regime not in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN, MarketRegime.MOMENTUM}:
        return Direction.NONE

    values = closes(candles_4h)
    ema_fast = ema(values, config.ema_fast)[-1]
    ema_mid = ema(values, config.ema_mid)[-1]
    ema_slow = ema(values, config.ema_slow)[-1]
    close = values[-1]

    if close > ema_slow and ema_fast > ema_mid > ema_slow:
        return Direction.LONG
    if close < ema_slow and ema_fast < ema_mid < ema_slow:
        return Direction.SHORT
    return Direction.NONE


def _pullback_depth_atr(
    direction: Direction,
    candles_1h: list[Candle],
    ema_mid_1h: Decimal,
    atr_1h: Decimal,
    lookback: int = 8,
) -> Decimal:
    if atr_1h <= 0:
        return Decimal("0")
    recent = candles_1h[-lookback:]
    if direction == Direction.LONG:
        pullback_low = min(c.low for c in recent)
        return max(Decimal("0"), (ema_mid_1h - pullback_low) / atr_1h)
    if direction == Direction.SHORT:
        pullback_high = max(c.high for c in recent)
        return max(Decimal("0"), (pullback_high - ema_mid_1h) / atr_1h)
    return Decimal("0")


def _continuation_confirmed(direction: Direction, candles_15m: list[Candle], ema_fast_15m: Decimal) -> bool:
    if len(candles_15m) < 2:
        return False
    previous = candles_15m[-2]
    current = candles_15m[-1]
    if direction == Direction.LONG:
        return previous.close <= ema_fast_15m and current.close > ema_fast_15m and current.close > current.open
    if direction == Direction.SHORT:
        return previous.close >= ema_fast_15m and current.close < ema_fast_15m and current.close < current.open
    return False


def _directional_flow_ok(direction: Direction, metrics: MarketMetrics) -> bool:
    if direction == Direction.LONG:
        if metrics.order_book_imbalance < Decimal("0"):
            return False
        return metrics.taker_buy_ratio is None or metrics.taker_buy_ratio >= Decimal("0.50")
    if direction == Direction.SHORT:
        if metrics.order_book_imbalance > Decimal("0"):
            return False
        return metrics.taker_buy_ratio is None or metrics.taker_buy_ratio <= Decimal("0.50")
    return False


class TrendPullbackStrategy:
    """Shadow candidate for continuation entries after a controlled trend pullback."""

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
        min_required = max(self.config.ema_slow, self.config.volume_lookback, self.config.atr_period) + 5
        if len(candles_15m) < min_required or len(candles_1h) < min_required or len(candles_4h) < min_required:
            return None

        regime = self.regime_detector.detect(candles_4h)
        if regime.trend_strength < self.config.trend_pullback_min_trend_strength:
            return None

        direction = _trend_direction(candles_4h, self.config, regime.regime)
        if direction == Direction.NONE:
            return None

        values_1h = closes(candles_1h)
        values_15m = closes(candles_15m)
        ema_mid_1h = to_decimal(ema(values_1h, self.config.ema_mid)[-1])
        ema_fast_15m = to_decimal(ema(values_15m, self.config.ema_fast)[-1])
        atr_1h = to_decimal(atr(candles_1h, self.config.atr_period)[-1])
        atr_15m = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        entry = candles_15m[-1].close
        if atr_1h <= 0 or atr_15m <= 0 or entry <= 0:
            return None

        depth_atr = _pullback_depth_atr(direction, candles_1h, ema_mid_1h, atr_1h)
        if depth_atr < self.config.trend_pullback_min_depth_atr:
            return None
        if depth_atr > self.config.trend_pullback_max_depth_atr:
            return None

        volume_ratio = _volume_ratio(candles_15m, self.config.volume_lookback)
        if volume_ratio < self.config.trend_pullback_min_volume_ratio:
            return None

        rsi_value = to_decimal(rsi(values_15m, self.config.rsi_period)[-1])
        if direction == Direction.LONG and not (Decimal("45") <= rsi_value <= Decimal("68")):
            return None
        if direction == Direction.SHORT and not (Decimal("32") <= rsi_value <= Decimal("55")):
            return None

        continuation_ok = _continuation_confirmed(direction, candles_15m, ema_fast_15m)
        if not continuation_ok:
            return None

        flow_ok = _directional_flow_ok(direction, metrics)
        if not flow_ok:
            return None

        edge_snapshot = self.edge_analyzer.analyze(candles_15m, direction, metrics) if self.edge_analyzer else None
        edge_score = edge_snapshot.score if edge_snapshot else Decimal("0")
        edge_ok = edge_score >= self.config.trend_pullback_min_edge_score

        confluence_flags = [
            "htf_trend",
            "pullback_depth",
            "continuation",
            "volume",
            "flow",
        ]
        if edge_ok:
            confluence_flags.append("edge")
        if regime.regime == MarketRegime.MOMENTUM:
            confluence_flags.append("momentum_regime")
        confluence = len(confluence_flags)
        if confluence < self.config.trend_pullback_min_confluence:
            return None

        stop_distance = atr_15m * self.config.trend_pullback_stop_atr_multiplier
        rr = self.config.trend_pullback_take_profit_rr
        if direction == Direction.LONG:
            stop_loss = entry - stop_distance
            take_profit = entry + stop_distance * rr
        else:
            stop_loss = entry + stop_distance
            take_profit = entry - stop_distance * rr
        if stop_loss <= 0 or take_profit <= 0:
            return None

        confidence = Decimal("0.50")
        confidence += min(regime.trend_strength / Decimal("10"), Decimal("0.12"))
        if volume_ratio >= Decimal("1.5"):
            confidence += Decimal("0.08")
        if edge_ok:
            confidence += min(edge_score * Decimal("0.15"), Decimal("0.10"))
        if regime.regime == MarketRegime.MOMENTUM:
            confidence += Decimal("0.05")

        atr_pct = atr_15m / entry * Decimal("100")
        return Signal(
            symbol=symbol,
            direction=direction,
            style=TradingStyle.INTRADAY,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=min(confidence, Decimal("0.86")),
            reason=(
                f"TREND_PULLBACK: regime={regime.regime.value}, depth_atr={depth_atr:.2f}, "
                f"vol_ratio={volume_ratio:.2f}, rsi={rsi_value:.2f}, confluence={confluence}"
            ),
            metadata={
                "strategy": "TREND_PULLBACK",
                "regime": regime.regime.value,
                "trend_strength": str(regime.trend_strength),
                "pullback_depth_atr": str(depth_atr),
                "volume_ratio": str(volume_ratio),
                "rsi": str(rsi_value),
                "atr_pct": str(atr_pct),
                "rr": str(rr),
                "edge_score": str(edge_score),
                "edge_reasons": list(edge_snapshot.reasons) if edge_snapshot else [],
                "trend_pullback_confluence": str(confluence),
                "trend_pullback_flags": list(confluence_flags),
                "spread_bps": str(metrics.spread_bps),
                "taker_buy_ratio": str(metrics.taker_buy_ratio) if metrics.taker_buy_ratio is not None else None,
                "order_book_imbalance": str(metrics.order_book_imbalance),
                "hour_utc": str((candles_15m[-1].close_time // 3_600_000) % 24),
            },
        )
