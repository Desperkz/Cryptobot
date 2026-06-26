from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from trading_bot.config import MarketFilterConfig
from trading_bot.models import Candle, Direction, Signal, to_decimal


@dataclass(frozen=True)
class FilterDecision:
    allowed: bool
    reason: str = ""


class MarketEntryFilter:
    def __init__(self, config: MarketFilterConfig) -> None:
        self.config = config

    def btc_4h_change(self, candles: list[Candle]) -> Decimal | None:
        if len(candles) < 2:
            return None
        # MarketDataProvider strips the currently forming candle, so the last
        # two candles here are the two latest completed 4h bars.
        previous = candles[-2].close
        current = candles[-1].close
        if previous <= 0:
            return None
        return (current - previous) / previous

    def allow_signal(
        self,
        signal: Signal,
        btc_4h_change: Decimal | None,
        self_learning_thresholds: dict[str, Any] | None = None,
        oi_change_pct: Decimal | None = None,
    ) -> FilterDecision:
        if self.config.btc_4h_drop_filter_enabled and btc_4h_change is not None:
            btc_weak = btc_4h_change <= self.config.btc_4h_max_drop_pct
            if btc_weak and self.config.block_all_when_btc_weak:
                return FilterDecision(False, f"BTC 4h change {btc_4h_change:.2%} below filter.")
            if btc_weak and self.config.block_longs_when_btc_weak and signal.direction == Direction.LONG:
                return FilterDecision(False, f"BTC 4h change {btc_4h_change:.2%}; long entries blocked.")

        if self.config.use_open_interest_filter and oi_change_pct is not None:
            if oi_change_pct <= self.config.oi_drop_cascade_pct:
                return FilterDecision(False, f"OI drop {oi_change_pct:.2f}% — possible liquidation cascade.")
            if oi_change_pct >= self.config.oi_rise_longs_blocked_pct and signal.direction.value == "LONG":
                return FilterDecision(False, f"OI surge {oi_change_pct:.2f}% + LONG — longs at risk.")
        metadata = signal.metadata
        hour = _int_or_none(metadata.get("hour_utc"))
        if self.config.utc_session_filter_enabled and hour in self.config.avoid_utc_hours:
            if not self._session_override_allowed(signal, hour):
                return FilterDecision(False, f"UTC hour {hour} is in avoided session filter.")

        if self.config.use_self_learning_filters and self_learning_thresholds:
            decision = self._self_learning_decision(signal, self_learning_thresholds)
            if not decision.allowed:
                return decision

        return FilterDecision(True)

    def _session_override_allowed(self, signal: Signal, hour: int | None) -> bool:
        if not self.config.high_confidence_squeeze_session_override:
            return False
        if hour not in self.config.high_confidence_squeeze_allowed_hours:
            return False
        metadata = signal.metadata
        if str(metadata.get("strategy", "")).upper() not in {"SQUEEZE_BREAKOUT", "SQUEEZE_BREAKOUT_DYNAMIC"}:
            return False
        if str(metadata.get("squeeze_state", "")).lower() != "release":
            return False
        return signal.confidence >= self.config.high_confidence_squeeze_min_confidence

    def _self_learning_decision(self, signal: Signal, thresholds: dict[str, Any]) -> FilterDecision:
        metadata = signal.metadata
        hour = str(metadata.get("hour_utc", "unknown"))
        symbol_hour = f"{signal.symbol}@{hour}UTC"
        if signal.symbol in thresholds.get("blocked_symbols", []):
            return FilterDecision(False, f"Self-learning blocked symbol {signal.symbol}.")
        if symbol_hour in thresholds.get("blocked_symbol_hours", []):
            return FilterDecision(False, f"Self-learning blocked segment {symbol_hour}.")

        atr_bucket = _bucket(metadata.get("atr_pct"), suffix="%")
        if atr_bucket and atr_bucket in thresholds.get("avoid_atr_pct_buckets", []):
            return FilterDecision(False, f"Self-learning blocked ATR bucket {atr_bucket}.")
        rsi_bucket = _bucket(metadata.get("rsi"), step=Decimal("4"), suffix="")
        if rsi_bucket and rsi_bucket in thresholds.get("avoid_rsi_buckets", []):
            return FilterDecision(False, f"Self-learning blocked RSI bucket {rsi_bucket}.")
        return FilterDecision(True)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bucket(value: Any, step: Decimal = Decimal("0.4"), suffix: str = "") -> str | None:
    if value in (None, "", "None"):
        return None
    decimal_value = to_decimal(value)
    low = (decimal_value / step).to_integral_value(rounding=ROUND_FLOOR) * step
    high = low + step
    return f"{low}-{high}{suffix}"
