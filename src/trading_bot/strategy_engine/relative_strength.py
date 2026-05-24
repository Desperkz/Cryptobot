from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_bot.models import Candle, Direction


@dataclass(frozen=True)
class RelativeStrengthAnnotation:
    symbol_change_4h: Decimal
    btc_change_4h: Decimal | None
    relative_change_4h: Decimal | None
    symbol_change_24h: Decimal | None
    alignment: str
    score: Decimal
    reasons: tuple[str, ...]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "symbol_change_4h": str(self.symbol_change_4h),
            "btc_change_4h": str(self.btc_change_4h) if self.btc_change_4h is not None else None,
            "relative_change_4h": str(self.relative_change_4h) if self.relative_change_4h is not None else None,
            "symbol_change_24h": str(self.symbol_change_24h) if self.symbol_change_24h is not None else None,
            "alignment": self.alignment,
            "score": str(self.score),
            "reasons": list(self.reasons),
        }


def annotate_relative_strength(
    candles_4h: list[Candle],
    direction: Direction,
    btc_change_4h: Decimal | None,
    *,
    threshold: Decimal = Decimal("0.002"),
) -> RelativeStrengthAnnotation:
    symbol_change_4h = _pct_change(candles_4h, 1) or Decimal("0")
    symbol_change_24h = _pct_change(candles_4h, 6)
    relative_change_4h = symbol_change_4h - btc_change_4h if btc_change_4h is not None else None
    reasons: list[str] = []

    if relative_change_4h is None:
        alignment = "unknown"
        score = Decimal("0.50")
        reasons.append("missing_btc_benchmark")
    elif direction == Direction.LONG:
        if relative_change_4h >= threshold:
            alignment = "aligned"
            reasons.append("long_relative_strength")
        elif relative_change_4h <= -threshold:
            alignment = "against"
            reasons.append("long_relative_weakness")
        else:
            alignment = "neutral"
            reasons.append("relative_neutral")
        score = _score(relative_change_4h, alignment)
    elif direction == Direction.SHORT:
        if relative_change_4h <= -threshold:
            alignment = "aligned"
            reasons.append("short_relative_weakness")
        elif relative_change_4h >= threshold:
            alignment = "against"
            reasons.append("short_relative_strength")
        else:
            alignment = "neutral"
            reasons.append("relative_neutral")
        score = _score(-relative_change_4h, alignment)
    else:
        alignment = "unknown"
        score = Decimal("0.50")
        reasons.append("no_direction")

    if symbol_change_24h is not None:
        if symbol_change_24h > Decimal("0"):
            reasons.append("positive_24h_momentum")
        elif symbol_change_24h < Decimal("0"):
            reasons.append("negative_24h_momentum")

    return RelativeStrengthAnnotation(
        symbol_change_4h=symbol_change_4h,
        btc_change_4h=btc_change_4h,
        relative_change_4h=relative_change_4h,
        symbol_change_24h=symbol_change_24h,
        alignment=alignment,
        score=score,
        reasons=tuple(reasons),
    )


def _pct_change(candles: list[Candle], periods: int) -> Decimal | None:
    if len(candles) <= periods:
        return None
    previous = candles[-(periods + 1)].close
    current = candles[-1].close
    if previous <= 0:
        return None
    return (current - previous) / previous


def _score(direction_relative_change: Decimal, alignment: str) -> Decimal:
    magnitude = min(abs(direction_relative_change) / Decimal("0.02"), Decimal("1"))
    if alignment == "aligned":
        return min(Decimal("0.50") + magnitude * Decimal("0.40"), Decimal("0.95"))
    if alignment == "against":
        return max(Decimal("0.50") - magnitude * Decimal("0.40"), Decimal("0.05"))
    return Decimal("0.50")
