from __future__ import annotations

from decimal import Decimal

from trading_bot.config import EdgeFilterConfig
from trading_bot.models import Candle, Direction, EdgeSnapshot, MarketMetrics


class EdgeAnalyzer:
    """Price-action and order-flow confirmation layer.

    This is still rule-based research logic, not a guarantee of alpha. It makes
    the strategy less dependent on EMA/RSI by requiring observable microstructure
    clues: sweep, absorption, aggressive flow, structure break, and liquidation
    zone proximity.
    """

    def __init__(self, config: EdgeFilterConfig) -> None:
        self.config = config

    def analyze(self, candles_15m: list[Candle], direction: Direction, metrics: MarketMetrics) -> EdgeSnapshot:
        lookback = min(self.config.liquidity_sweep_lookback, max(len(candles_15m) - 2, 1))
        recent = candles_15m[-lookback - 1 : -1]
        current = candles_15m[-1]

        sweep, sweep_direction = self._liquidity_sweep(current, recent)
        absorption, absorption_direction = self._absorption(current)
        structure_break, structure_direction = self._structure_break(candles_15m)
        liquidation_nearby = self._liquidation_zone_nearby(current, recent)
        flow_ok = _directional_delta_ok(direction, metrics.aggressive_buy_sell_delta, self.config.aggressive_flow_delta_min)

        score = Decimal("0")
        reasons: list[str] = []
        if sweep and sweep_direction == direction:
            score += Decimal("0.22")
            reasons.append("liquidity_sweep")
        if absorption and absorption_direction == direction:
            score += Decimal("0.22")
            reasons.append("absorption")
        if structure_break and structure_direction == direction:
            score += Decimal("0.24")
            reasons.append("structure_break")
        if flow_ok:
            score += Decimal("0.18")
            reasons.append("aggressive_flow")
        if liquidation_nearby:
            score += Decimal("0.14")
            reasons.append("liquidation_zone_proxy")

        return EdgeSnapshot(
            liquidity_sweep=sweep,
            sweep_direction=sweep_direction,
            absorption=absorption,
            absorption_direction=absorption_direction,
            structure_break=structure_break,
            structure_direction=structure_direction,
            liquidation_zone_nearby=liquidation_nearby,
            score=min(score, Decimal("1")),
            reasons=tuple(reasons),
        )

    def _liquidity_sweep(self, current: Candle, recent: list[Candle]) -> tuple[bool, Direction]:
        if not recent:
            return False, Direction.NONE
        prior_high = max(c.high for c in recent)
        prior_low = min(c.low for c in recent)
        threshold = current.close * self.config.liquidity_sweep_threshold_bps / Decimal("10000")
        if current.low < prior_low - threshold and current.close > prior_low:
            return True, Direction.LONG
        if current.high > prior_high + threshold and current.close < prior_high:
            return True, Direction.SHORT
        return False, Direction.NONE

    def _absorption(self, candle: Candle) -> tuple[bool, Direction]:
        body = abs(candle.close - candle.open)
        body = max(body, candle.close * Decimal("0.0001"))
        lower_wick = min(candle.open, candle.close) - candle.low
        upper_wick = candle.high - max(candle.open, candle.close)
        ratio = self.config.absorption_wick_body_ratio
        if lower_wick / body >= ratio and candle.close >= candle.open:
            return True, Direction.LONG
        if upper_wick / body >= ratio and candle.close <= candle.open:
            return True, Direction.SHORT
        return False, Direction.NONE

    def _structure_break(self, candles: list[Candle]) -> tuple[bool, Direction]:
        lookback = min(self.config.structure_break_lookback, max(len(candles) - 2, 1))
        prior = candles[-lookback - 1 : -1]
        current = candles[-1]
        if not prior:
            return False, Direction.NONE
        if current.close > max(c.high for c in prior):
            return True, Direction.LONG
        if current.close < min(c.low for c in prior):
            return True, Direction.SHORT
        return False, Direction.NONE

    def _liquidation_zone_nearby(self, current: Candle, recent: list[Candle]) -> bool:
        if not recent:
            return False
        recent_high = max(c.high for c in recent)
        recent_low = min(c.low for c in recent)
        distance_bps = self.config.liquidation_zone_distance_bps
        high_distance = abs(recent_high - current.close) / current.close * Decimal("10000")
        low_distance = abs(current.close - recent_low) / current.close * Decimal("10000")
        return high_distance <= distance_bps or low_distance <= distance_bps


def _directional_delta_ok(direction: Direction, delta: Decimal, minimum: Decimal) -> bool:
    if direction == Direction.LONG:
        return delta >= minimum
    if direction == Direction.SHORT:
        return delta <= -minimum
    return False

