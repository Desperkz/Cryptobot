from __future__ import annotations

from decimal import Decimal

from trading_bot.bot import _shadow_candidate_context_rejection_reason
from trading_bot.models import Direction, Signal, TradingStyle


def signal(strategy: str, order_flow: dict) -> Signal:
    return Signal(
        symbol="ADAUSDT",
        direction=Direction.SHORT,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("1.0"),
        stop_loss=Decimal("1.1"),
        take_profit=Decimal("0.9"),
        confidence=Decimal("0.55"),
        reason="test",
        metadata={"strategy": strategy, "order_flow": order_flow},
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


def test_vwr_watch_shadow_context_blocks_dangerous_liquidity_flags() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("VWAP_REVERSION_WATCH", order_flow(score="0.80", flags=["liquidation_cascade"]))
    )

    assert reason is not None
    assert "dangerous liquidity context" in reason


def test_grid_shadow_context_blocks_against_order_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("RANGE_GRID", order_flow(alignment="against", score="0.50"))
    )

    assert reason is not None
    assert "against range fade" in reason


def test_shadow_context_gate_ignores_non_candidate_strategy() -> None:
    assert _shadow_candidate_context_rejection_reason(signal("SQUEEZE_BREAKOUT", order_flow(alignment="against"))) is None
