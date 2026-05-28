from __future__ import annotations

from decimal import Decimal

from trading_bot.bot import _shadow_candidate_context_rejection_reason
from trading_bot.models import Direction, Signal, TradingStyle


def signal(
    strategy: str,
    order_flow: dict,
    *,
    symbol: str = "ADAUSDT",
    direction: Direction = Direction.SHORT,
    relative_strength: dict | None = None,
) -> Signal:
    metadata = {"strategy": strategy, "order_flow": order_flow}
    if relative_strength is not None:
        metadata["relative_strength"] = relative_strength
    return Signal(
        symbol=symbol,
        direction=direction,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("1.0"),
        stop_loss=Decimal("1.1"),
        take_profit=Decimal("0.9"),
        confidence=Decimal("0.55"),
        reason="test",
        metadata=metadata,
    )


def order_flow(*, alignment: str = "aligned", score: str = "0.70", flags: list[str] | None = None) -> dict:
    return {
        "alignment": alignment,
        "score": score,
        "risk_flags": flags or [],
    }


def test_lsr_shadow_context_blocks_adverse_liquidity() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "LIQUIDITY_SWEEP_REVERSAL",
            order_flow(score="0.78", flags=["adverse_liquidity_nearby"]),
        )
    )

    assert reason is not None
    assert "adverse liquidity" in reason


def test_lsr_shadow_context_blocks_weak_order_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("LIQUIDITY_SWEEP_REVERSAL", order_flow(score="0.42"))
    )

    assert reason is not None
    assert "not strong enough" in reason


def test_lsr_shadow_context_allows_research_follow_through_sample() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("LIQUIDITY_SWEEP_REVERSAL", order_flow(score="0.68"))
    ) is None


def test_vwr_watch_shadow_context_blocks_dangerous_liquidity_flags() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("VWAP_REVERSION_WATCH", order_flow(score="0.80", flags=["liquidation_cascade"]))
    )

    assert reason is not None
    assert "dangerous liquidity context" in reason


def test_vwap_shadow_context_requires_clean_reversion_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("VWAP_REVERSION", order_flow(score="0.38"))
    )

    assert reason is not None
    assert "not clean enough" in reason


def test_vwap_watch_shadow_allows_research_sample_with_nearby_liquidity() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("VWAP_REVERSION_WATCH", order_flow(score="0.46", flags=["adverse_liquidity_nearby"]))
    ) is None


def test_grid_shadow_context_blocks_against_order_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("RANGE_GRID", order_flow(alignment="against", score="0.50"))
    )

    assert reason is not None
    assert "against range fade" in reason


def test_grid_shadow_context_blocks_dangerous_range_edge_flags() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("RANGE_GRID", order_flow(alignment="aligned", score="0.68", flags=["structure_break_against"]))
    )

    assert reason is not None
    assert "dangerous range-edge flow" in reason


def test_grid_shadow_context_allows_research_sample_with_minor_flow_flags() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("RANGE_GRID", order_flow(alignment="aligned", score="0.34", flags=["adverse_liquidity_nearby"]))
    ) is None


def test_sqz_dynamic_shadow_blocks_order_flow_against_breakout() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("SQUEEZE_BREAKOUT_DYNAMIC", order_flow(alignment="against", score="0.18", flags=["taker_flow_against"]))
    )

    assert reason is not None
    assert "against breakout" in reason


def test_sqz_dynamic_shadow_requires_retest_for_mixed_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("SQUEEZE_BREAKOUT_DYNAMIC", order_flow(alignment="mixed", score="0.58"))
    )

    assert reason is not None
    assert "retest confirmation" in reason


def test_sqz_dynamic_shadow_allows_clean_aligned_flow() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("SQUEEZE_BREAKOUT_DYNAMIC", order_flow(alignment="aligned", score="0.70"))
    ) is None


def test_tpb_shadow_blocks_toxic_symbol_from_retest_quarantine() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("TREND_PULLBACK", order_flow(alignment="aligned", score="0.70"), symbol="LTCUSDT")
    )

    assert reason is not None
    assert "retest quarantine" in reason


def test_tpb_shadow_blocks_mixed_order_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("TREND_PULLBACK", order_flow(alignment="mixed", score="0.70"))
    )

    assert reason is not None
    assert "needs aligned order-flow" in reason


def test_tpb_shadow_blocks_short_without_relative_weakness_confirmation() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "unknown"},
        )
    )

    assert reason is not None
    assert "relative-weakness confirmation" in reason


def test_tpb_shadow_allows_clean_long_continuation() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.62"),
            direction=Direction.LONG,
            relative_strength={"alignment": "unknown"},
        )
    ) is None


def test_shadow_context_gate_ignores_non_candidate_strategy() -> None:
    assert _shadow_candidate_context_rejection_reason(signal("SQUEEZE_BREAKOUT", order_flow(alignment="against"))) is None
