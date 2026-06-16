from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from trading_bot.config import AnalyticsConfig
from trading_bot.models import LearningRecommendation, to_decimal


@dataclass(frozen=True)
class SegmentStats:
    key: str
    trades: int
    winrate: Decimal
    expectancy_r: Decimal
    avg_r: Decimal


class SelfLearningEngine:
    """Post-trade diagnostics that turn trade history into explicit rules.

    This does not mutate live risk by itself. It produces recommendations such
    as "SOL during 00-06 UTC has poor expectancy" or "ATR > 2.2% is bad" that
    can be reviewed and promoted into config or ML training data.
    """

    def __init__(self, config: AnalyticsConfig) -> None:
        self.config = config

    def analyze(self, trades: list[dict[str, Any]]) -> list[LearningRecommendation]:
        closed_trades = [trade for trade in trades if str(trade.get("status") or "").upper() == "CLOSED"]
        if not closed_trades:
            return []
        recommendations: list[LearningRecommendation] = []
        for scope, buckets in self._segments(closed_trades).items():
            for key, rows in buckets.items():
                stats = self._stats(key, rows)
                if stats.trades < self.config.segment_min_trades:
                    continue
                if stats.expectancy_r <= self.config.bad_segment_expectancy_r:
                    recommendations.append(
                        LearningRecommendation(
                            scope=scope,
                            key=key,
                            metric="expectancy_r",
                            value=stats.expectancy_r,
                            recommendation=self._recommendation(scope, key, stats),
                            trades=stats.trades,
                        )
                    )
        return sorted(recommendations, key=lambda item: (item.value, -item.trades))

    def adaptive_thresholds(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        recommendations = self.analyze(trades)
        blocked_symbols = [
            rec.key
            for rec in recommendations
            if rec.scope == "symbol" and rec.trades >= self.config.disable_symbol_after_bad_trades
        ]
        blocked_hours = [rec.key for rec in recommendations if rec.scope == "symbol_hour"]
        bad_atr_buckets = [rec.key for rec in recommendations if rec.scope == "atr_pct_bucket"]
        bad_rsi_buckets = [rec.key for rec in recommendations if rec.scope == "rsi_bucket"]
        return {
            "blocked_symbols": blocked_symbols,
            "blocked_symbol_hours": blocked_hours,
            "avoid_atr_pct_buckets": bad_atr_buckets,
            "avoid_rsi_buckets": bad_rsi_buckets,
            "recommendations": [rec.__dict__ for rec in recommendations],
        }

    def _segments(self, trades: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
        segments: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for trade in trades:
            metadata = _metadata(trade)
            signal_metadata = metadata.get("signal_metadata", metadata)
            symbol = str(trade.get("symbol", "UNKNOWN"))
            hour = str(signal_metadata.get("hour_utc", "unknown"))
            atr = _maybe_decimal(signal_metadata.get("atr_pct"))
            rsi = _maybe_decimal(signal_metadata.get("rsi"))
            style = str(signal_metadata.get("style", trade.get("style", "unknown")))
            edge_reasons = signal_metadata.get("edge_reasons") or []

            segments["symbol"][symbol].append(trade)
            segments["symbol_hour"][f"{symbol}@{hour}UTC"].append(trade)
            segments["style"][style].append(trade)
            if atr is not None:
                segments["atr_pct_bucket"][_bucket(atr, self.config.atr_bucket_size_pct, "%")].append(trade)
            if rsi is not None:
                segments["rsi_bucket"][_bucket(rsi, Decimal(self.config.rsi_bucket_size), "")].append(trade)
            for reason in edge_reasons:
                segments["edge_reason"][str(reason)].append(trade)
        return segments

    def _stats(self, key: str, trades: list[dict[str, Any]]) -> SegmentStats:
        r_values = [_r_multiple(trade) for trade in trades]
        wins = [r for r in r_values if r > 0]
        winrate = Decimal(len(wins)) / Decimal(len(r_values)) if r_values else Decimal("0")
        expectancy = sum(r_values, Decimal("0")) / Decimal(len(r_values)) if r_values else Decimal("0")
        return SegmentStats(key, len(r_values), winrate, expectancy, expectancy)

    def _recommendation(self, scope: str, key: str, stats: SegmentStats) -> str:
        if scope == "symbol_hour":
            return f"Avoid or down-weight {key}; expectancy is {stats.expectancy_r:.2f}R over {stats.trades} trades."
        if scope == "atr_pct_bucket":
            return f"Avoid ATR bucket {key} or widen/skip entries there; expectancy is {stats.expectancy_r:.2f}R."
        if scope == "rsi_bucket":
            return f"Avoid RSI bucket {key} or require stronger confirmation; expectancy is {stats.expectancy_r:.2f}R."
        if scope == "edge_reason":
            return f"Edge condition {key} is underperforming; validate or lower its weight."
        if scope == "style":
            return f"Strategy/style {key} is underperforming; route less capital or disable until retested."
        return f"Reduce exposure for {scope}={key}; expectancy is {stats.expectancy_r:.2f}R."


def _metadata(trade: dict[str, Any]) -> dict[str, Any]:
    raw = trade.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _r_multiple(trade: dict[str, Any]) -> Decimal:
    pnl = to_decimal(trade.get("realized_pnl", "0") or "0")
    risk = to_decimal(trade.get("risk_amount", "0") or "0")
    if risk <= 0:
        entry = to_decimal(trade.get("entry_price", "0") or "0")
        stop = to_decimal(trade.get("stop_loss", "0") or "0")
        qty = to_decimal(trade.get("quantity", "0") or "0")
        risk = abs(entry - stop) * qty
    return pnl / risk if risk > 0 else Decimal("0")


def _maybe_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    return to_decimal(value)


def _bucket(value: Decimal, step: Decimal, suffix: str) -> str:
    low = (value / step).to_integral_value(rounding=ROUND_FLOOR) * step
    high = low + step
    return f"{low}-{high}{suffix}"
