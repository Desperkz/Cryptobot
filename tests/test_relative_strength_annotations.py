from __future__ import annotations

from decimal import Decimal

from trading_bot.models import Candle, Direction
from trading_bot.strategy_engine.relative_strength import annotate_relative_strength


def candle(i: int, close: str) -> Candle:
    value = Decimal(close)
    return Candle(
        open_time=i,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1000"),
        close_time=i + 1,
        quote_volume=value * Decimal("1000"),
    )


def test_relative_strength_marks_aligned_long() -> None:
    candles = [candle(i, str(100 + i)) for i in range(7)]

    annotation = annotate_relative_strength(candles, Direction.LONG, Decimal("0.005"))

    assert annotation.alignment == "aligned"
    assert annotation.relative_change_4h is not None
    assert annotation.relative_change_4h > Decimal("0.002")
    assert annotation.score > Decimal("0.50")
    assert "long_relative_strength" in annotation.reasons


def test_relative_strength_marks_against_short_when_symbol_outperforms_btc() -> None:
    candles = [candle(i, str(100 + i)) for i in range(7)]

    annotation = annotate_relative_strength(candles, Direction.SHORT, Decimal("0.000"))

    assert annotation.alignment == "against"
    assert annotation.score < Decimal("0.50")
    assert "short_relative_strength" in annotation.reasons


def test_relative_strength_is_unknown_without_btc_benchmark() -> None:
    candles = [candle(i, str(100 + i)) for i in range(7)]

    annotation = annotate_relative_strength(candles, Direction.LONG, None)

    assert annotation.alignment == "unknown"
    assert annotation.score == Decimal("0.50")
    assert "missing_btc_benchmark" in annotation.reasons
