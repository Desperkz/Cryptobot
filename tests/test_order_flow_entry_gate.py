from decimal import Decimal

from trading_bot.bot import _order_flow_entry_rejection_reason
from trading_bot.models import Direction, Signal, TradingStyle


def signal(
    strategy: str,
    *,
    alignment: str,
    score: str,
    flags: list[str] | None = None,
    relative_strength: str = "aligned",
    retest: bool = True,
    state: str = "release",
    timing: str = "release_followthrough",
    breakout_atr: str = "1.20",
    reasons: list[str] | None = None,
) -> Signal:
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
                "reasons": reasons if reasons is not None else ["structure_break_aligned"],
                "risk_flags": flags or [],
            },
            "relative_strength": {"alignment": relative_strength},
            "squeeze_retest_confirmed": retest,
            "squeeze_state": state,
            "squeeze_entry_timing": timing,
            "breakout_atr": breakout_atr,
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


def test_squeeze_blocks_missing_relative_strength_confirmation() -> None:
    rejected = _order_flow_entry_rejection_reason(
        signal("SQUEEZE_BREAKOUT", alignment="aligned", score="0.78", relative_strength="unknown")
    )

    assert rejected is not None
    assert rejected[0] == "RELATIVE_STRENGTH"
    assert "relative-strength confirmation" in rejected[1]


def test_squeeze_blocks_no_retest_when_release_is_not_strong_enough() -> None:
    rejected = _order_flow_entry_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT",
            alignment="aligned",
            score="0.70",
            retest=False,
            state="build",
            timing="early_breakout",
            breakout_atr="0.40",
        )
    )

    assert rejected is not None
    assert rejected[0] == "SQZ_RETEST"
    assert "no retest" in rejected[1]


def test_squeeze_allows_strong_clean_release_without_retest() -> None:
    assert _order_flow_entry_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT",
            alignment="aligned",
            score="0.76",
            retest=False,
            state="release",
            timing="release_followthrough",
            breakout_atr="1.80",
        )
    ) is None


def test_squeeze_blocks_without_structure_break_confirmation() -> None:
    rejected = _order_flow_entry_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT",
            alignment="aligned",
            score="0.72",
            reasons=["taker_flow_aligned", "book_imbalance_aligned", "aggressive_delta_aligned"],
        )
    )

    assert rejected is not None
    assert rejected[0] == "STRUCTURE_BREAK"
    assert "structure-break confirmation" in rejected[1]


def test_squeeze_blocks_absorption_against_even_with_aligned_flow() -> None:
    rejected = _order_flow_entry_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT",
            alignment="aligned",
            score="0.80",
            flags=["absorption_against"],
            retest=True,
        )
    )

    assert rejected is not None
    assert rejected[0] == "ORDER_FLOW"
    assert "absorption against breakout" in rejected[1]


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
