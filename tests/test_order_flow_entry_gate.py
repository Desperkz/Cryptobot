from decimal import Decimal

from trading_bot.bot import _order_flow_entry_rejection_reason
from trading_bot.models import Direction, Signal, TradingStyle


def signal(strategy: str, *, alignment: str, score: str, flags: list[str] | None = None) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("99"),
        take_profit=Decimal("103"),
        confidence=Decimal("0.80"),
        reason="test",
        metadata={
            "strategy": strategy,
            "order_flow": {
                "alignment": alignment,
                "score": score,
                "risk_flags": flags or [],
            },
        },
    )


def test_squeeze_blocks_order_flow_against_breakout() -> None:
    rejected = _order_flow_entry_rejection_reason(
        signal("SQUEEZE_BREAKOUT", alignment="against", score="0.50", flags=["taker_flow_against"])
    )

    assert rejected is not None
    assert rejected[0] == "ORDER_FLOW"
    assert "against breakout" in rejected[1]


def test_squeeze_allows_clean_aligned_order_flow() -> None:
    assert _order_flow_entry_rejection_reason(
        signal("SQUEEZE_BREAKOUT", alignment="aligned", score="0.72")
    ) is None


def test_lsr_blocks_adverse_liquidity_nearby() -> None:
    rejected = _order_flow_entry_rejection_reason(
        signal("LIQUIDITY_SWEEP_REVERSAL", alignment="aligned", score="0.90", flags=["adverse_liquidity_nearby"])
    )

    assert rejected is not None
    assert "adverse liquidity" in rejected[1]


def test_grid_blocks_hostile_range_flow() -> None:
    rejected = _order_flow_entry_rejection_reason(
        signal("RANGE_GRID", alignment="mixed", score="0.70", flags=["structure_break_against"])
    )

    assert rejected is not None
    assert "unsafe range-flow" in rejected[1]
