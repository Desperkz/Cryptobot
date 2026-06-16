from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_bot.config import StrategyConfig
from trading_bot.market_regime_detector import MarketRegimeDetector
from trading_bot.models import Candle, Direction, MarketMetrics, MarketRegime, Signal, TradingStyle, to_decimal
from trading_bot.strategy_engine.edge import EdgeAnalyzer
from trading_bot.strategy_engine.indicators import atr, closes, ema, rsi, volumes, rolling_average


def _volume_ratio(candles: list[Candle], lookback: int) -> Decimal:
    volume_values = volumes(candles)
    avg_volume = rolling_average(volume_values, lookback)[-1]
    return to_decimal(volume_values[-1] / avg_volume) if avg_volume > 0 else Decimal("0")


def _atr_pct(atr_value: Decimal, entry: Decimal) -> Decimal:
    return atr_value / entry * Decimal("100") if entry > 0 else Decimal("0")


def _build_signal(
    *,
    symbol: str,
    strategy: str,
    direction: Direction,
    entry: Decimal,
    stop_distance: Decimal,
    rr: Decimal,
    confidence: Decimal,
    reason: str,
    metadata: dict,
) -> Signal | None:
    if direction == Direction.LONG:
        stop_loss = entry - stop_distance
        take_profit = entry + stop_distance * rr
    elif direction == Direction.SHORT:
        stop_loss = entry + stop_distance
        take_profit = entry - stop_distance * rr
    else:
        return None
    if entry <= 0 or stop_loss <= 0 or take_profit <= 0:
        return None
    return Signal(
        symbol=symbol,
        direction=direction,
        style=TradingStyle.INTRADAY,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=min(confidence, Decimal("0.88")),
        reason=reason,
        metadata={"strategy": strategy, **metadata},
    )


class LiquiditySweepReversalStrategy:
    """Shadow candidate: fade stop-hunt sweeps only after price closes back inside the range."""

    def __init__(self, config: StrategyConfig, edge_analyzer: EdgeAnalyzer | None = None) -> None:
        self.config = config
        self.edge_analyzer = edge_analyzer

    def generate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> Signal | None:
        lookback = self.config.liquidity_sweep_lookback
        if len(candles_15m) < max(lookback + 2, self.config.atr_period + 2):
            return None

        prior = candles_15m[-lookback - 1 : -1]
        current = candles_15m[-1]
        prior_high = max(c.high for c in prior)
        prior_low = min(c.low for c in prior)
        atr_15m = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        entry = current.close
        if atr_15m <= 0 or entry <= 0:
            return None

        direction = Direction.NONE
        if current.low < prior_low and current.close > prior_low and current.close > current.open:
            direction = Direction.LONG
        elif current.high > prior_high and current.close < prior_high and current.close < current.open:
            direction = Direction.SHORT
        if direction == Direction.NONE:
            return None
        reclaim_distance = (current.close - prior_low) if direction == Direction.LONG else (prior_high - current.close)
        min_reclaim_atr = self.config.liquidity_sweep_min_reclaim_atr
        reclaim_atr = reclaim_distance / atr_15m
        if reclaim_atr < min_reclaim_atr:
            return None
        follow_through_body_atr = _sweep_follow_through_body_atr(
            candles_15m,
            direction,
            atr_15m,
            self.config.liquidity_sweep_follow_through_min_body_atr,
        )
        if follow_through_body_atr is None:
            return None

        volume_ratio = _volume_ratio(candles_15m, self.config.volume_lookback)
        if volume_ratio < Decimal("1.25"):
            return None
        if not _strict_directional_flow_confirmed(metrics, direction):
            return None

        edge_snapshot = self.edge_analyzer.analyze(candles_15m, direction, metrics) if self.edge_analyzer else None
        edge_score = edge_snapshot.score if edge_snapshot else Decimal("0")
        if edge_score < self.config.liquidity_sweep_min_edge_score:
            return None
        edge_reasons = set(edge_snapshot.reasons) if edge_snapshot else set()
        if not {"liquidity_sweep", "absorption", "aggressive_flow"}.issubset(edge_reasons):
            return None

        rr = self.config.liquidity_sweep_take_profit_rr
        stop_distance = max(atr_15m * self.config.liquidity_sweep_stop_atr_multiplier, entry * Decimal("0.001"))
        confidence = Decimal("0.58") + min(edge_score * Decimal("0.18"), Decimal("0.12"))
        if volume_ratio >= Decimal("1.25"):
            confidence += Decimal("0.05")

        return _build_signal(
            symbol=symbol,
            strategy="LIQUIDITY_SWEEP_REVERSAL",
            direction=direction,
            entry=entry,
            stop_distance=stop_distance,
            rr=rr,
            confidence=confidence,
            reason=(
                f"LIQUIDITY_SWEEP_REVERSAL: edge={edge_score:.2f}, "
                f"vol_ratio={volume_ratio:.2f}, sweep={direction.value}"
            ),
            metadata={
                "edge_score": str(edge_score),
                "edge_reasons": list(edge_snapshot.reasons) if edge_snapshot else [],
                "volume_ratio": str(volume_ratio),
                "atr_pct": str(_atr_pct(atr_15m, entry)),
                "hour_utc": str((candles_15m[-1].close_time // 3_600_000) % 24),
                "prior_high": str(prior_high),
                "prior_low": str(prior_low),
                "reclaim_atr": str(reclaim_atr),
                "min_reclaim_atr": str(min_reclaim_atr),
                "follow_through_body_atr": str(follow_through_body_atr),
                "follow_through_confirmed": "True",
                "rr": str(rr),
            },
        )


class VwapReversionStrategy:
    """Shadow candidate: intraday reversion to rolling VWAP after ATR-sized stretch."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

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

    def generate_watch(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> Signal | None:
        signal, _diagnostic = self.evaluate_watch(symbol, candles_15m, candles_1h, candles_4h, metrics)
        return signal

    def evaluate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> tuple[Signal | None, dict[str, Any]]:
        return self._evaluate_variant(
            symbol,
            candles_15m,
            candles_1h,
            candles_4h,
            metrics,
            strategy="VWAP_REVERSION",
            variant="safe",
            min_deviation_atr=self.config.vwap_reversion_deviation_atr,
            max_deviation_atr=self.config.vwap_reversion_max_deviation_atr,
            min_volume_ratio=self.config.vwap_reversion_min_volume_ratio,
            confidence_base=Decimal("0.58"),
        )

    def evaluate_watch(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> tuple[Signal | None, dict[str, Any]]:
        return self._evaluate_variant(
            symbol,
            candles_15m,
            candles_1h,
            candles_4h,
            metrics,
            strategy="VWAP_REVERSION_WATCH",
            variant="watch",
            min_deviation_atr=self.config.vwap_reversion_watch_deviation_atr,
            max_deviation_atr=self.config.vwap_reversion_watch_max_deviation_atr,
            min_volume_ratio=self.config.vwap_reversion_watch_min_volume_ratio,
            confidence_base=Decimal("0.54"),
        )

    def _evaluate_variant(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
        *,
        strategy: str,
        variant: str,
        min_deviation_atr: Decimal,
        max_deviation_atr: Decimal,
        min_volume_ratio: Decimal,
        confidence_base: Decimal,
    ) -> tuple[Signal | None, dict[str, Any]]:
        del candles_1h, candles_4h

        diagnostic: dict[str, Any] = {
            "strategy": strategy,
            "variant": variant,
            "symbol": symbol,
            "decision": "NO_SIGNAL",
            "block_reason": "not_evaluated",
            "min_deviation_atr": str(min_deviation_atr),
            "max_deviation_atr": str(max_deviation_atr),
            "min_volume_ratio": str(min_volume_ratio),
        }
        lookback = self.config.vwap_reversion_lookback
        if len(candles_15m) < max(lookback, self.config.atr_period + 2, self.config.rsi_period + 2):
            diagnostic["block_reason"] = "insufficient_candles"
            diagnostic["candles_15m"] = len(candles_15m)
            return None, diagnostic

        window = candles_15m[-lookback:]
        volume_sum = sum(c.volume for c in window)
        if volume_sum <= 0:
            diagnostic["block_reason"] = "zero_volume"
            return None, diagnostic
        vwap = sum(((c.high + c.low + c.close) / Decimal("3")) * c.volume for c in window) / volume_sum
        atr_15m = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        entry = candles_15m[-1].close
        if atr_15m <= 0 or entry <= 0:
            diagnostic["block_reason"] = "invalid_atr_or_entry"
            diagnostic.update({"atr": str(atr_15m), "entry": str(entry)})
            return None, diagnostic
        deviation_atr = (entry - vwap) / atr_15m
        previous_deviation_atr = (candles_15m[-2].close - vwap) / atr_15m
        reversion_progress_atr = abs(previous_deviation_atr) - abs(deviation_atr)
        rsi_value = to_decimal(rsi(closes(candles_15m), self.config.rsi_period)[-1])
        volume_ratio = _volume_ratio(candles_15m, self.config.volume_lookback)
        abs_deviation_atr = abs(deviation_atr)
        atr_pct = _atr_pct(atr_15m, entry)
        diagnostic.update(
            {
                "vwap": str(vwap),
                "entry": str(entry),
                "atr": str(atr_15m),
                "deviation_atr": str(deviation_atr),
                "previous_deviation_atr": str(previous_deviation_atr),
                "reversion_progress_atr": str(reversion_progress_atr),
                "abs_deviation_atr": str(abs_deviation_atr),
                "rsi": str(rsi_value),
                "volume_ratio": str(volume_ratio),
                "atr_pct": str(atr_pct),
                "taker_buy_ratio": str(metrics.taker_buy_ratio) if metrics.taker_buy_ratio is not None else None,
                "aggressive_delta": str(metrics.aggressive_buy_sell_delta),
                "book_imbalance": str(metrics.order_book_imbalance),
            }
        )

        direction = Direction.NONE
        if deviation_atr <= -min_deviation_atr and rsi_value <= Decimal("38"):
            direction = Direction.LONG
        elif deviation_atr >= min_deviation_atr and rsi_value >= Decimal("62"):
            direction = Direction.SHORT
        diagnostic["direction"] = direction.value
        if direction == Direction.NONE:
            diagnostic["block_reason"] = "below_min_deviation_or_rsi"
            return None, diagnostic

        if abs_deviation_atr > max_deviation_atr:
            diagnostic["block_reason"] = "extreme_deviation"
            return None, diagnostic
        if atr_pct < max(self.config.min_atr_pct, Decimal("0.20")):
            diagnostic["block_reason"] = "atr_too_low_for_vwap_reversion"
            return None, diagnostic
        if volume_ratio < min_volume_ratio:
            diagnostic["block_reason"] = "weak_volume"
            return None, diagnostic
        if not _vwap_reversal_confirmed(
            candles_15m,
            direction,
            atr_15m,
            self.config.vwap_reversion_reversal_min_body_atr,
        ):
            diagnostic["block_reason"] = "no_reversal_confirmation"
            return None, diagnostic
        min_progress_atr = (
            self.config.vwap_reversion_watch_min_progress_atr
            if variant == "watch"
            else self.config.vwap_reversion_min_progress_atr
        )
        diagnostic["min_progress_atr"] = str(min_progress_atr)
        if reversion_progress_atr < min_progress_atr:
            diagnostic["block_reason"] = "no_vwap_reversion_progress"
            return None, diagnostic
        flow_ok, flow_reason = _vwap_reversion_flow_quality(metrics, direction)
        if not flow_ok:
            diagnostic["block_reason"] = flow_reason
            return None, diagnostic
        stop_distance = atr_15m * self.config.vwap_reversion_stop_atr_multiplier
        rr = self.config.vwap_reversion_take_profit_rr
        confidence = confidence_base + min(abs_deviation_atr / Decimal("50"), Decimal("0.06"))
        if volume_ratio >= Decimal("1.1"):
            confidence += Decimal("0.04")
        diagnostic.update({"decision": "SIGNAL", "block_reason": "passed", "confidence": str(confidence)})

        signal = _build_signal(
            symbol=symbol,
            strategy=strategy,
            direction=direction,
            entry=entry,
            stop_distance=stop_distance,
            rr=rr,
            confidence=confidence,
            reason=f"{strategy}: deviation_atr={deviation_atr:.2f}, rsi={rsi_value:.2f}, variant={variant}",
            metadata={
                "vwap": str(vwap),
                "deviation_atr": str(deviation_atr),
                "max_deviation_atr": str(max_deviation_atr),
                "rsi": str(rsi_value),
                "volume_ratio": str(volume_ratio),
                "min_volume_ratio": str(min_volume_ratio),
                "previous_deviation_atr": str(previous_deviation_atr),
                "reversion_progress_atr": str(reversion_progress_atr),
                "min_progress_atr": str(min_progress_atr),
                "reversal_min_body_atr": str(self.config.vwap_reversion_reversal_min_body_atr),
                "reversal_confirmed": "True",
                "flow_confirmed": "True",
                "vwap_variant": variant,
                "atr_pct": str(atr_pct),
                "hour_utc": str((candles_15m[-1].close_time // 3_600_000) % 24),
                "rr": str(rr),
            },
        )
        return signal, diagnostic


def _sweep_follow_through_body_atr(
    candles: list[Candle],
    direction: Direction,
    atr_value: Decimal,
    min_body_atr: Decimal,
) -> Decimal | None:
    if len(candles) < 2 or atr_value <= 0:
        return None
    last = candles[-1]
    previous = candles[-2]
    body_atr = abs(last.close - last.open) / atr_value
    if body_atr < min_body_atr:
        return None
    candle_range = max(last.high - last.low, atr_value * Decimal("0.0001"))
    if direction == Direction.LONG:
        close_location = (last.close - last.low) / candle_range
        if last.close > last.open and last.close > previous.close and close_location >= Decimal("0.60"):
            return body_atr
    elif direction == Direction.SHORT:
        close_location = (last.high - last.close) / candle_range
        if last.close < last.open and last.close < previous.close and close_location >= Decimal("0.60"):
            return body_atr
    return None


def _vwap_reversal_confirmed(
    candles: list[Candle],
    direction: Direction,
    atr_value: Decimal,
    min_body_atr: Decimal,
) -> bool:
    if len(candles) < 2:
        return False
    last = candles[-1]
    previous = candles[-2]
    body = abs(last.close - last.open)
    lower_wick = min(last.open, last.close) - last.low
    upper_wick = last.high - max(last.open, last.close)
    body_atr = body / atr_value if atr_value > 0 else Decimal("0")
    min_wick = max(body * Decimal("2.0"), atr_value * Decimal("0.20"))

    if direction == Direction.LONG:
        bounce_close = body_atr >= min_body_atr and last.close > last.open and last.close > previous.close
        capitulation_wick = lower_wick >= min_wick and last.close >= last.open
        return bounce_close or capitulation_wick
    if direction == Direction.SHORT:
        rejection_close = body_atr >= min_body_atr and last.close < last.open and last.close < previous.close
        exhaustion_wick = upper_wick >= min_wick and last.close <= last.open
        return rejection_close or exhaustion_wick
    return False


def _vwap_flow_confirmed(metrics: MarketMetrics, direction: Direction) -> bool:
    taker_buy_ratio = metrics.taker_buy_ratio
    delta = metrics.aggressive_buy_sell_delta
    book_imbalance = metrics.order_book_imbalance
    if direction == Direction.LONG:
        if taker_buy_ratio is not None and taker_buy_ratio < Decimal("0.50"):
            return False
        if delta < Decimal("-0.05"):
            return False
        if book_imbalance < Decimal("-0.10"):
            return False
        return True
    if direction == Direction.SHORT:
        if taker_buy_ratio is not None and taker_buy_ratio > Decimal("0.50"):
            return False
        if delta > Decimal("0.05"):
            return False
        if book_imbalance > Decimal("0.10"):
            return False
        return True
    return False


def _vwap_reversion_flow_quality(metrics: MarketMetrics, direction: Direction) -> tuple[bool, str]:
    if not _vwap_flow_confirmed(metrics, direction):
        return False, "flow_against_reversion"

    taker_buy_ratio = metrics.taker_buy_ratio
    delta = metrics.aggressive_buy_sell_delta
    book_imbalance = metrics.order_book_imbalance
    if direction == Direction.LONG:
        if taker_buy_ratio is not None and taker_buy_ratio > Decimal("0.92"):
            return False, "flow_too_one_sided_for_reversion"
        if delta > Decimal("0.85") or book_imbalance > Decimal("0.85"):
            return False, "flow_too_one_sided_for_reversion"
    elif direction == Direction.SHORT:
        if taker_buy_ratio is not None and taker_buy_ratio < Decimal("0.08"):
            return False, "flow_too_one_sided_for_reversion"
        if delta < Decimal("-0.85") or book_imbalance < Decimal("-0.85"):
            return False, "flow_too_one_sided_for_reversion"
    return True, "passed"


def _strict_directional_flow_confirmed(metrics: MarketMetrics, direction: Direction) -> bool:
    taker_buy_ratio = metrics.taker_buy_ratio
    delta = metrics.aggressive_buy_sell_delta
    book_imbalance = metrics.order_book_imbalance
    if direction == Direction.LONG:
        if taker_buy_ratio is not None and taker_buy_ratio < Decimal("0.54"):
            return False
        return delta >= Decimal("0.10") and book_imbalance >= Decimal("0.04")
    if direction == Direction.SHORT:
        if taker_buy_ratio is not None and taker_buy_ratio > Decimal("0.46"):
            return False
        return delta <= Decimal("-0.10") and book_imbalance <= Decimal("-0.04")
    return False


class MomentumContinuationStrategy:
    """Shadow candidate: continuation after structure break, volume expansion, and trend alignment."""

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
        if len(candles_15m) < min_required or len(candles_4h) < min_required:
            return None

        regime = self.regime_detector.detect(candles_4h)
        if regime.regime not in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN, MarketRegime.MOMENTUM}:
            return None

        values_4h = closes(candles_4h)
        ema_mid_4h = to_decimal(ema(values_4h, self.config.ema_mid)[-1])
        ema_slow_4h = to_decimal(ema(values_4h, self.config.ema_slow)[-1])
        entry = candles_15m[-1].close
        direction = Direction.NONE
        if entry > ema_mid_4h > ema_slow_4h:
            direction = Direction.LONG
        elif entry < ema_mid_4h < ema_slow_4h:
            direction = Direction.SHORT
        if direction == Direction.NONE:
            return None

        recent = candles_15m[-21:-1]
        current = candles_15m[-1]
        if direction == Direction.LONG and current.close <= max(c.high for c in recent):
            return None
        if direction == Direction.SHORT and current.close >= min(c.low for c in recent):
            return None

        atr_15m = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        if atr_15m <= 0:
            return None
        atr_pct = _atr_pct(atr_15m, entry)
        if atr_pct > Decimal("1.50"):
            return None
        prior_high = max(c.high for c in recent)
        prior_low = min(c.low for c in recent)
        breakout_extension_atr = (
            (current.close - prior_high) / atr_15m
            if direction == Direction.LONG
            else (prior_low - current.close) / atr_15m
        )
        if breakout_extension_atr > Decimal("0.80"):
            return None
        if not _strict_directional_flow_confirmed(metrics, direction):
            return None
        body_atr = abs(current.close - current.open) / atr_15m
        if body_atr > Decimal("1.40"):
            return None

        volume_ratio = _volume_ratio(candles_15m, self.config.volume_lookback)
        if volume_ratio < self.config.momentum_continuation_min_volume_ratio:
            return None

        edge_snapshot = self.edge_analyzer.analyze(candles_15m, direction, metrics) if self.edge_analyzer else None
        edge_score = edge_snapshot.score if edge_snapshot else Decimal("0")
        if edge_score < self.config.momentum_continuation_min_edge_score:
            return None

        stop_distance = atr_15m * self.config.momentum_continuation_stop_atr_multiplier
        rr = self.config.momentum_continuation_take_profit_rr
        confidence = Decimal("0.56") + min(regime.trend_strength / Decimal("20"), Decimal("0.08"))
        if volume_ratio >= Decimal("1.8"):
            confidence += Decimal("0.05")
        confidence += min(edge_score * Decimal("0.12"), Decimal("0.08"))

        return _build_signal(
            symbol=symbol,
            strategy="MOMENTUM_CONTINUATION",
            direction=direction,
            entry=entry,
            stop_distance=stop_distance,
            rr=rr,
            confidence=confidence,
            reason=(
                f"MOMENTUM_CONTINUATION: regime={regime.regime.value}, "
                f"vol_ratio={volume_ratio:.2f}, edge={edge_score:.2f}"
            ),
            metadata={
                "regime": regime.regime.value,
                "trend_strength": str(regime.trend_strength),
                "volume_ratio": str(volume_ratio),
                "edge_score": str(edge_score),
                "edge_reasons": list(edge_snapshot.reasons) if edge_snapshot else [],
                "body_atr": str(body_atr),
                "breakout_extension_atr": str(breakout_extension_atr),
                "atr_pct": str(atr_pct),
                "hour_utc": str((candles_15m[-1].close_time // 3_600_000) % 24),
                "rr": str(rr),
            },
        )


class RangeGridStrategy:
    """Cautious shadow-only range fade candidate. Never promote without separate review."""

    def __init__(self, config: StrategyConfig, regime_detector: MarketRegimeDetector) -> None:
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
        lookback = self.config.range_grid_lookback
        if len(candles_15m) < max(lookback, self.config.atr_period + 2, self.config.rsi_period + 2):
            return None

        regime = self.regime_detector.detect(candles_4h)
        if regime.regime != MarketRegime.RANGE:
            return None

        window = candles_15m[-lookback:]
        range_high = max(c.high for c in window)
        range_low = min(c.low for c in window)
        entry = candles_15m[-1].close
        range_size = range_high - range_low
        atr_15m = to_decimal(atr(candles_15m, self.config.atr_period)[-1])
        if entry <= 0 or range_size <= 0 or atr_15m <= 0:
            return None
        if range_size < atr_15m * Decimal("2.20") or range_size > atr_15m * Decimal("8"):
            return None
        if metrics.spread_bps > Decimal("5"):
            return None

        position_in_range = (entry - range_low) / range_size
        rsi_value = to_decimal(rsi(closes(candles_15m), self.config.rsi_period)[-1])
        entry_zone = self.config.range_grid_entry_zone_pct
        direction = Direction.NONE
        if position_in_range <= entry_zone and rsi_value <= self.config.range_grid_rsi_long_max:
            direction = Direction.LONG
        elif position_in_range >= Decimal("1") - entry_zone and rsi_value >= self.config.range_grid_rsi_short_min:
            direction = Direction.SHORT
        if direction == Direction.NONE:
            return None
        if not _range_grid_flow_safe(metrics, direction):
            return None

        stop_distance = atr_15m * self.config.range_grid_stop_atr_multiplier
        rr = self.config.range_grid_take_profit_rr
        confidence = Decimal("0.50")
        if metrics.spread_bps <= Decimal("8"):
            confidence += Decimal("0.04")

        return _build_signal(
            symbol=symbol,
            strategy="RANGE_GRID",
            direction=direction,
            entry=entry,
            stop_distance=stop_distance,
            rr=rr,
            confidence=confidence,
            reason=(
                f"RANGE_GRID: pos={position_in_range:.2f}, rsi={rsi_value:.2f}, "
                f"range_atr={(range_size / atr_15m):.2f}"
            ),
            metadata={
                "regime": regime.regime.value,
                "range_high": str(range_high),
                "range_low": str(range_low),
                "range_position": str(position_in_range),
                "entry_zone": str(entry_zone),
                "range_atr": str(range_size / atr_15m),
                "rsi": str(rsi_value),
                "spread_bps": str(metrics.spread_bps),
                "flow_safe": "True",
                "atr_pct": str(_atr_pct(atr_15m, entry)),
                "hour_utc": str((candles_15m[-1].close_time // 3_600_000) % 24),
                "rr": str(rr),
                "caution": "shadow_only_range_grid",
            },
        )


def _range_grid_flow_safe(metrics: MarketMetrics, direction: Direction) -> bool:
    taker_buy_ratio = metrics.taker_buy_ratio
    delta = metrics.aggressive_buy_sell_delta
    book_imbalance = metrics.order_book_imbalance
    if direction == Direction.LONG:
        if taker_buy_ratio is not None and (taker_buy_ratio < Decimal("0.12") or taker_buy_ratio > Decimal("0.82")):
            return False
        if delta < Decimal("-0.55") or book_imbalance < Decimal("-0.45"):
            return False
        if delta > Decimal("0.85") or book_imbalance > Decimal("0.85"):
            return False
    elif direction == Direction.SHORT:
        if taker_buy_ratio is not None and (taker_buy_ratio > Decimal("0.88") or taker_buy_ratio < Decimal("0.18")):
            return False
        if delta > Decimal("0.55") or book_imbalance > Decimal("0.45"):
            return False
        if delta < Decimal("-0.85") or book_imbalance < Decimal("-0.85"):
            return False
    else:
        return False
    return True
