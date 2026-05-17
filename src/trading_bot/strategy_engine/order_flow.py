from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_bot.config import EdgeFilterConfig
from trading_bot.models import Candle, Direction, MarketMetrics


@dataclass(frozen=True)
class OrderFlowAnnotation:
    """Research-only order-flow/liquidation context for a generated signal."""

    flow_bias: Direction
    alignment: str
    score: Decimal
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    liquidity_side: str
    distance_to_upper_liquidity_bps: Decimal | None
    distance_to_lower_liquidity_bps: Decimal | None
    taker_buy_ratio: Decimal | None
    order_book_imbalance: Decimal
    aggressive_delta: Decimal
    open_interest_change_pct: Decimal | None
    funding_rate: Decimal | None
    sweep_direction: Direction
    absorption_direction: Direction
    structure_break_direction: Direction

    def to_metadata(self) -> dict[str, Any]:
        return {
            "flow_bias": self.flow_bias.value,
            "alignment": self.alignment,
            "score": _to_float(self.score),
            "reasons": list(self.reasons),
            "risk_flags": list(self.risk_flags),
            "liquidity_side": self.liquidity_side,
            "distance_to_upper_liquidity_bps": _to_float(self.distance_to_upper_liquidity_bps),
            "distance_to_lower_liquidity_bps": _to_float(self.distance_to_lower_liquidity_bps),
            "taker_buy_ratio": _to_float(self.taker_buy_ratio),
            "order_book_imbalance": _to_float(self.order_book_imbalance),
            "aggressive_delta": _to_float(self.aggressive_delta),
            "open_interest_change_pct": _to_float(self.open_interest_change_pct),
            "funding_rate": _to_float(self.funding_rate),
            "sweep_direction": self.sweep_direction.value,
            "absorption_direction": self.absorption_direction.value,
            "structure_break_direction": self.structure_break_direction.value,
        }


class OrderFlowAnnotator:
    """Annotates existing strategy signals with order-flow research evidence.

    The annotator deliberately does not accept or reject trades. It turns
    already-fetched Binance microstructure metrics into a consistent payload
    that can later be reviewed in scorecards, dashboards, and ML datasets.
    """

    def __init__(self, config: EdgeFilterConfig) -> None:
        self.config = config

    def annotate(
        self,
        candles_15m: list[Candle],
        direction: Direction,
        metrics: MarketMetrics,
    ) -> OrderFlowAnnotation:
        if not candles_15m or direction not in {Direction.LONG, Direction.SHORT}:
            return self._empty(metrics)

        current = candles_15m[-1]
        recent = self._recent(candles_15m)
        score = Decimal("0")
        reasons: list[str] = []
        risk_flags: list[str] = []
        long_votes = 0
        short_votes = 0
        contra_votes = 0

        taker_bias = self._taker_bias(metrics.taker_buy_ratio)
        if taker_bias == Direction.LONG:
            long_votes += 1
        elif taker_bias == Direction.SHORT:
            short_votes += 1
        if taker_bias == direction:
            score += Decimal("0.20")
            reasons.append("taker_flow_aligned")
        elif taker_bias != Direction.NONE:
            contra_votes += 1
            risk_flags.append("taker_flow_against")

        book_bias = self._book_bias(metrics.order_book_imbalance)
        if book_bias == Direction.LONG:
            long_votes += 1
        elif book_bias == Direction.SHORT:
            short_votes += 1
        if book_bias == direction:
            score += Decimal("0.18")
            reasons.append("book_imbalance_aligned")
        elif book_bias != Direction.NONE:
            contra_votes += 1
            risk_flags.append("book_imbalance_against")

        delta_bias = self._delta_bias(metrics.aggressive_buy_sell_delta)
        if delta_bias == Direction.LONG:
            long_votes += 1
        elif delta_bias == Direction.SHORT:
            short_votes += 1
        if delta_bias == direction:
            score += Decimal("0.20")
            reasons.append("aggressive_delta_aligned")
        elif delta_bias != Direction.NONE:
            contra_votes += 1
            risk_flags.append("aggressive_delta_against")

        oi_threshold = max(abs(self.config.open_interest_change_min_pct), Decimal("0.10"))
        if metrics.open_interest_change_pct is not None:
            if metrics.open_interest_change_pct >= oi_threshold:
                score += Decimal("0.14")
                reasons.append("open_interest_expansion")
            elif metrics.open_interest_change_pct <= -oi_threshold:
                risk_flags.append("liquidation_cascade")
                contra_votes += 1

        funding_flag = self._funding_risk(direction, metrics.funding_rate)
        if funding_flag:
            risk_flags.append(funding_flag)

        sweep, sweep_direction = self._liquidity_sweep(current, recent)
        if sweep and sweep_direction == direction:
            score += Decimal("0.10")
            reasons.append("liquidity_sweep_aligned")
        elif sweep and sweep_direction != Direction.NONE:
            risk_flags.append("liquidity_sweep_against")
            contra_votes += 1

        absorption, absorption_direction = self._absorption(current)
        if absorption and absorption_direction == direction:
            score += Decimal("0.10")
            reasons.append("absorption_aligned")
        elif absorption and absorption_direction != Direction.NONE:
            risk_flags.append("absorption_against")

        structure_break, structure_direction = self._structure_break(candles_15m)
        if structure_break and structure_direction == direction:
            score += Decimal("0.08")
            reasons.append("structure_break_aligned")
        elif structure_break and structure_direction != Direction.NONE:
            risk_flags.append("structure_break_against")

        upper_distance, lower_distance = self._liquidity_distances(current, recent)
        liquidity_side = self._liquidity_side(upper_distance, lower_distance)
        if self._target_liquidity_near(direction, liquidity_side):
            score += Decimal("0.10")
            reasons.append("target_liquidity_nearby")
        elif self._adverse_liquidity_near(direction, liquidity_side):
            risk_flags.append("adverse_liquidity_nearby")

        flow_bias = Direction.NONE
        if long_votes > short_votes:
            flow_bias = Direction.LONG
        elif short_votes > long_votes:
            flow_bias = Direction.SHORT

        score = min(max(score, Decimal("0")), Decimal("1"))
        alignment = self._alignment(score, contra_votes, risk_flags)
        return OrderFlowAnnotation(
            flow_bias=flow_bias,
            alignment=alignment,
            score=score,
            reasons=tuple(dict.fromkeys(reasons)),
            risk_flags=tuple(dict.fromkeys(risk_flags)),
            liquidity_side=liquidity_side,
            distance_to_upper_liquidity_bps=upper_distance,
            distance_to_lower_liquidity_bps=lower_distance,
            taker_buy_ratio=metrics.taker_buy_ratio,
            order_book_imbalance=metrics.order_book_imbalance,
            aggressive_delta=metrics.aggressive_buy_sell_delta,
            open_interest_change_pct=metrics.open_interest_change_pct,
            funding_rate=metrics.funding_rate,
            sweep_direction=sweep_direction,
            absorption_direction=absorption_direction,
            structure_break_direction=structure_direction,
        )

    def _empty(self, metrics: MarketMetrics) -> OrderFlowAnnotation:
        return OrderFlowAnnotation(
            flow_bias=Direction.NONE,
            alignment="mixed",
            score=Decimal("0"),
            reasons=(),
            risk_flags=("insufficient_context",),
            liquidity_side="none",
            distance_to_upper_liquidity_bps=None,
            distance_to_lower_liquidity_bps=None,
            taker_buy_ratio=metrics.taker_buy_ratio,
            order_book_imbalance=metrics.order_book_imbalance,
            aggressive_delta=metrics.aggressive_buy_sell_delta,
            open_interest_change_pct=metrics.open_interest_change_pct,
            funding_rate=metrics.funding_rate,
            sweep_direction=Direction.NONE,
            absorption_direction=Direction.NONE,
            structure_break_direction=Direction.NONE,
        )

    def _recent(self, candles: list[Candle]) -> list[Candle]:
        lookback = min(self.config.structure_break_lookback, max(len(candles) - 1, 1))
        return candles[-lookback - 1 : -1]

    def _taker_bias(self, ratio: Decimal | None) -> Direction:
        if ratio is None:
            return Direction.NONE
        if ratio >= self.config.taker_buy_ratio_long_min:
            return Direction.LONG
        if ratio <= self.config.taker_buy_ratio_short_max:
            return Direction.SHORT
        return Direction.NONE

    def _book_bias(self, imbalance: Decimal) -> Direction:
        if imbalance >= self.config.order_book_imbalance_min:
            return Direction.LONG
        if imbalance <= -self.config.order_book_imbalance_min:
            return Direction.SHORT
        return Direction.NONE

    def _delta_bias(self, delta: Decimal) -> Direction:
        if delta >= self.config.aggressive_flow_delta_min:
            return Direction.LONG
        if delta <= -self.config.aggressive_flow_delta_min:
            return Direction.SHORT
        return Direction.NONE

    def _funding_risk(self, direction: Direction, funding_rate: Decimal | None) -> str | None:
        if funding_rate is None:
            return None
        threshold = Decimal("0.0005")
        if direction == Direction.LONG and funding_rate >= threshold:
            return "crowded_long_funding"
        if direction == Direction.SHORT and funding_rate <= -threshold:
            return "crowded_short_funding"
        return None

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
        body = max(abs(candle.close - candle.open), candle.close * Decimal("0.0001"))
        lower_wick = min(candle.open, candle.close) - candle.low
        upper_wick = candle.high - max(candle.open, candle.close)
        ratio = self.config.absorption_wick_body_ratio
        if lower_wick / body >= ratio and candle.close >= candle.open:
            return True, Direction.LONG
        if upper_wick / body >= ratio and candle.close <= candle.open:
            return True, Direction.SHORT
        return False, Direction.NONE

    def _structure_break(self, candles: list[Candle]) -> tuple[bool, Direction]:
        recent = self._recent(candles)
        if not recent:
            return False, Direction.NONE
        current = candles[-1]
        if current.close > max(c.high for c in recent):
            return True, Direction.LONG
        if current.close < min(c.low for c in recent):
            return True, Direction.SHORT
        return False, Direction.NONE

    def _liquidity_distances(self, current: Candle, recent: list[Candle]) -> tuple[Decimal | None, Decimal | None]:
        if not recent or current.close <= 0:
            return None, None
        recent_high = max(c.high for c in recent)
        recent_low = min(c.low for c in recent)
        upper = abs(recent_high - current.close) / current.close * Decimal("10000")
        lower = abs(current.close - recent_low) / current.close * Decimal("10000")
        return upper, lower

    def _liquidity_side(self, upper_bps: Decimal | None, lower_bps: Decimal | None) -> str:
        threshold = self.config.liquidation_zone_distance_bps
        upper_near = upper_bps is not None and upper_bps <= threshold
        lower_near = lower_bps is not None and lower_bps <= threshold
        if upper_near and lower_near:
            return "both"
        if upper_near:
            return "upside"
        if lower_near:
            return "downside"
        return "none"

    def _target_liquidity_near(self, direction: Direction, side: str) -> bool:
        return (direction == Direction.LONG and side in {"upside", "both"}) or (
            direction == Direction.SHORT and side in {"downside", "both"}
        )

    def _adverse_liquidity_near(self, direction: Direction, side: str) -> bool:
        return (direction == Direction.LONG and side == "downside") or (
            direction == Direction.SHORT and side == "upside"
        )

    def _alignment(self, score: Decimal, contra_votes: int, risk_flags: list[str]) -> str:
        severe_flags = {"liquidation_cascade"}
        if severe_flags.intersection(risk_flags) and score < Decimal("0.55"):
            return "against"
        if contra_votes >= 2:
            return "against"
        if score >= Decimal("0.55") and contra_votes == 0:
            return "aligned"
        return "mixed"


def _to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(round(value, 8))
