from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from trading_bot.bot import _mean_reversion_context_rejection_reason
from trading_bot.models import Direction, Signal, TradingStyle
from trading_bot.strategy_engine.order_flow import OrderFlowAnnotation


def config(**overrides):
    values = {
        "mean_reversion_btc_direction_gate_enabled": True,
        "mean_reversion_btc_direction_gate_pct": Decimal("0.012"),
        "mean_reversion_order_flow_gate_enabled": True,
        "mean_reversion_min_order_flow_score": Decimal("0.25"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def signal(direction: Direction, strategy: str = "MEAN_REVERSION") -> Signal:
    return Signal(
        symbol="HYPEUSDT",
        direction=direction,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("99") if direction == Direction.LONG else Decimal("101"),
        take_profit=Decimal("102") if direction == Direction.LONG else Decimal("98"),
        confidence=Decimal("0.75"),
        reason="test",
        metadata={"strategy": strategy},
    )


def order_flow(
    *,
    alignment: str = "aligned",
    score: str = "0.55",
    flags: tuple[str, ...] = (),
) -> OrderFlowAnnotation:
    return OrderFlowAnnotation(
        flow_bias=Direction.NONE,
        alignment=alignment,
        score=Decimal(score),
        reasons=(),
        risk_flags=flags,
        liquidity_side="none",
        distance_to_upper_liquidity_bps=None,
        distance_to_lower_liquidity_bps=None,
        taker_buy_ratio=None,
        order_book_imbalance=Decimal("0"),
        aggressive_delta=Decimal("0"),
        open_interest_change_pct=None,
        funding_rate=None,
        sweep_direction=Direction.NONE,
        absorption_direction=Direction.NONE,
        structure_break_direction=Direction.NONE,
    )


def test_mr_context_gate_blocks_short_against_btc_squeeze() -> None:
    reason = _mean_reversion_context_rejection_reason(
        signal(Direction.SHORT),
        Decimal("0.018"),
        order_flow(),
        config(),
    )

    assert reason is not None
    assert reason[0] == "MR_CONTEXT"
    assert "BTC 4h impulse" in reason[1]
    assert "upward" in reason[1]


def test_mr_context_gate_blocks_long_against_btc_cascade() -> None:
    reason = _mean_reversion_context_rejection_reason(
        signal(Direction.LONG),
        Decimal("-0.018"),
        order_flow(),
        config(),
    )

    assert reason is not None
    assert reason[0] == "MR_CONTEXT"
    assert "downward" in reason[1]


def test_mr_context_gate_allows_btc_impulse_with_trade_direction() -> None:
    assert (
        _mean_reversion_context_rejection_reason(
            signal(Direction.SHORT),
            Decimal("-0.018"),
            order_flow(),
            config(),
        )
        is None
    )


def test_mr_context_gate_blocks_order_flow_against_signal() -> None:
    reason = _mean_reversion_context_rejection_reason(
        signal(Direction.LONG),
        Decimal("0"),
        order_flow(alignment="against", flags=("taker_flow_against",)),
        config(),
    )

    assert reason is not None
    assert reason[0] == "MR_CONTEXT"
    assert "order-flow alignment is against" in reason[1]


def test_mr_context_gate_blocks_weak_score_with_multiple_against_flags() -> None:
    reason = _mean_reversion_context_rejection_reason(
        signal(Direction.LONG),
        Decimal("0"),
        order_flow(score="0.10", flags=("taker_flow_against", "book_imbalance_against")),
        config(),
    )

    assert reason is not None
    assert reason[0] == "MR_CONTEXT"
    assert "weak order-flow score" in reason[1]


def test_mr_context_gate_ignores_non_mr_signals() -> None:
    assert (
        _mean_reversion_context_rejection_reason(
            signal(Direction.SHORT, strategy="SQUEEZE_BREAKOUT"),
            Decimal("0.03"),
            order_flow(alignment="against", score="0.0", flags=("liquidation_cascade",)),
            config(),
        )
        is None
    )
