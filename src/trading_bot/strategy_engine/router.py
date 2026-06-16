from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from trading_bot.models import Candle, MarketMetrics, Signal
from trading_bot.config import StrategyConfig
from trading_bot.models import Direction
from trading_bot.strategy_engine.candidate_strategies import (
    LiquiditySweepReversalStrategy,
    MomentumContinuationStrategy,
    RangeGridStrategy,
    VwapReversionStrategy,
)
from trading_bot.strategy_engine.mean_reversion import MeanReversionStrategy
from trading_bot.strategy_engine.multi_timeframe import MultiTimeframeStrategy
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy
from trading_bot.strategy_engine.trend_pullback import TrendPullbackStrategy


ROUTER_CONFLICT_MIN_CONFIDENCE_GAP = Decimal("0.08")


class StrategyRouter:
    def __init__(
        self,
        trend: MultiTimeframeStrategy,
        mean_reversion: MeanReversionStrategy,
        enabled_strategies: list[str],
        squeeze_breakout: SqueezeBreakoutStrategy | None = None,
        trend_pullback: TrendPullbackStrategy | None = None,
        liquidity_sweep_reversal: LiquiditySweepReversalStrategy | None = None,
        vwap_reversion: VwapReversionStrategy | None = None,
        momentum_continuation: MomentumContinuationStrategy | None = None,
        range_grid: RangeGridStrategy | None = None,
        shadow_strategies: list[str] | None = None,
        config: StrategyConfig | None = None,
    ) -> None:
        self.trend = trend
        self.mean_reversion = mean_reversion
        self.squeeze_breakout = squeeze_breakout
        self.trend_pullback = trend_pullback
        self.liquidity_sweep_reversal = liquidity_sweep_reversal
        self.vwap_reversion = vwap_reversion
        self.momentum_continuation = momentum_continuation
        self.range_grid = range_grid
        self.enabled = {name.upper() for name in enabled_strategies}
        self.shadow_enabled = {name.upper() for name in (shadow_strategies or [])}
        self.config = config
        self._diagnostics: list[dict[str, Any]] = []

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics = self._diagnostics
        self._diagnostics = []
        return diagnostics

    def _generate_candidates(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
        enabled: set[str],
    ) -> list[Signal]:
        candidates: list[Signal] = []

        if "MEAN_REVERSION" in enabled:
            signal = self.mean_reversion.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(signal)

        if "TREND_FOLLOWING" in enabled:
            if hasattr(self.trend, "evaluate"):
                signal, diagnostic = self.trend.evaluate(symbol, candles_15m, candles_1h, candles_4h, metrics)
                self._diagnostics.append(diagnostic)
            else:
                signal = self.trend.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(signal)

        if "SQUEEZE_BREAKOUT" in enabled and self.squeeze_breakout:
            signal = self.squeeze_breakout.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(signal)

        if "SQUEEZE_BREAKOUT_DYNAMIC" in enabled and self.squeeze_breakout:
            signal = self.squeeze_breakout.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(_as_squeeze_dynamic_variant(signal, "SQUEEZE_BREAKOUT_DYNAMIC"))

        if "SQUEEZE_BREAKOUT_DYNAMIC_UPD" in enabled and self.squeeze_breakout:
            signal = self.squeeze_breakout.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(_as_squeeze_dynamic_variant(signal, "SQUEEZE_BREAKOUT_DYNAMIC_UPD"))

        if "TREND_PULLBACK" in enabled and self.trend_pullback:
            signal = self.trend_pullback.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(signal)

        if "LIQUIDITY_SWEEP_REVERSAL" in enabled and self.liquidity_sweep_reversal:
            signal = self.liquidity_sweep_reversal.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(signal)

        if "VWAP_REVERSION" in enabled and self.vwap_reversion:
            signal, diagnostic = self.vwap_reversion.evaluate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            self._diagnostics.append(diagnostic)
            if signal:
                candidates.append(signal)

        if "VWAP_REVERSION_WATCH" in enabled and self.vwap_reversion:
            signal, diagnostic = self.vwap_reversion.evaluate_watch(symbol, candles_15m, candles_1h, candles_4h, metrics)
            self._diagnostics.append(diagnostic)
            if signal:
                candidates.append(signal)

        if "MOMENTUM_CONTINUATION" in enabled and self.momentum_continuation:
            signal = self.momentum_continuation.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(signal)

        if "RANGE_GRID" in enabled and self.range_grid:
            signal = self.range_grid.generate(symbol, candles_15m, candles_1h, candles_4h, metrics)
            if signal:
                candidates.append(signal)

        return [item for item in (self._apply_funding_carry_filter(signal, metrics) for signal in candidates) if item]

    def _apply_funding_carry_filter(self, signal: Signal, metrics: MarketMetrics) -> Signal | None:
        if not self.config or not self.config.funding_carry_filter_enabled:
            return signal
        if metrics is None:
            return signal
        funding_rate = metrics.funding_rate
        if funding_rate is None:
            return signal

        funding_rate = Decimal(str(funding_rate))
        bad_for_direction = (
            (signal.direction == Direction.LONG and funding_rate > 0)
            or (signal.direction == Direction.SHORT and funding_rate < 0)
        )
        if not bad_for_direction:
            metadata = {
                **dict(signal.metadata),
                "funding_carry": "favorable_or_neutral",
                "funding_rate": str(funding_rate),
            }
            return replace(signal, metadata=metadata)

        abs_funding = abs(funding_rate)
        if abs_funding >= self.config.funding_carry_block_threshold:
            return None
        if abs_funding >= self.config.funding_carry_penalty_threshold:
            strategy_name = str(signal.metadata.get("strategy", "")).upper()
            if strategy_name in {
                "LIQUIDITY_SWEEP_REVERSAL",
                "VWAP_REVERSION",
                "MOMENTUM_CONTINUATION",
                "TREND_FOLLOWING",
            }:
                return None
            metadata = {
                **dict(signal.metadata),
                "funding_carry": "penalized",
                "funding_rate": str(funding_rate),
            }
            confidence = max(Decimal("0.01"), signal.confidence - Decimal("0.05"))
            return replace(signal, confidence=confidence, metadata=metadata)
        return replace(
            signal,
            metadata={
                **dict(signal.metadata),
                "funding_carry": "neutral",
                "funding_rate": str(funding_rate),
            },
        )

    def _select_trade_signal(self, candidates: list[Signal]) -> Signal | None:
        if not candidates:
            return None

        ranked = sorted(candidates, key=lambda s: s.confidence, reverse=True)
        best = ranked[0]
        if len(ranked) >= 2:
            directions = {s.direction for s in ranked}
            if len(directions) > 1 and best.confidence - ranked[1].confidence < ROUTER_CONFLICT_MIN_CONFIDENCE_GAP:
                return None
            if len(directions) > 1:
                metadata = {
                    **dict(best.metadata or {}),
                    "direction_conflict_resolved": True,
                    "opposing_candidates": [
                        {
                            "strategy": str(candidate.metadata.get("strategy", "UNKNOWN")),
                            "direction": candidate.direction.value,
                            "confidence": str(candidate.confidence),
                        }
                        for candidate in ranked[1:]
                        if candidate.direction != best.direction
                    ],
                }
                return replace(best, metadata=metadata)

        return best

    def generate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> Signal | None:
        return self._select_trade_signal(
            self._generate_candidates(symbol, candles_15m, candles_1h, candles_4h, metrics, self.enabled)
        )

    def generate_shadow(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics,
    ) -> list[Signal]:
        return self._generate_candidates(symbol, candles_15m, candles_1h, candles_4h, metrics, self.shadow_enabled)


def _as_squeeze_dynamic_variant(signal: Signal, strategy: str) -> Signal:
    metadata = {
        **dict(signal.metadata or {}),
        "strategy": strategy,
        "parent_strategy": "SQUEEZE_BREAKOUT",
        "sizing_variant": "dynamic_challenger",
    }
    return replace(
        signal,
        metadata=metadata,
        reason=signal.reason.replace("SQUEEZE_BREAKOUT", strategy, 1)
        if "SQUEEZE_BREAKOUT" in signal.reason
        else f"{strategy}: {signal.reason}",
    )
