from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_bot.backtester.realistic_execution import (
    ExecutionAssumptions,
    estimate_quantity_for_risk,
    simulate_realistic_trade,
)
from trading_bot.models import Direction


@dataclass(frozen=True)
class Candle:
    open_time: int
    high: Decimal
    low: Decimal
    close: Decimal
    close_time: int


def candle(index: int, high: str, low: str, close: str) -> Candle:
    open_time = index * 3_600_000
    return Candle(
        open_time=open_time,
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        close_time=open_time + 3_600_000,
    )


def test_realistic_execution_applies_partial_tps_and_costs() -> None:
    assumptions = ExecutionAssumptions(
        taker_fee_bps=Decimal("4"),
        base_slippage_bps=Decimal("10"),
        random_slippage_bps=Decimal("0"),
        funding_bps_per_8h=Decimal("0"),
    )
    candles = [
        candle(0, "100", "100", "100"),
        candle(1, "103.5", "99", "103"),
        candle(2, "106", "102", "105.5"),
    ]

    result = simulate_realistic_trade(
        candles,
        0,
        Direction.LONG,
        entry=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        quantity=Decimal("1"),
        risk_amount=Decimal("5"),
        max_bars=2,
        assumptions=assumptions,
    )

    assert result.reason == "take_profit"
    assert result.filled_targets == ("TP1", "TP2")
    assert result.gross_pnl == Decimal("4.25")
    assert result.fees > 0
    assert result.slippage_cost > 0
    assert result.net_pnl < result.gross_pnl
    assert result.remaining_quantity == Decimal("0")


def test_pessimistic_intrabar_prefers_stop_when_target_and_stop_hit() -> None:
    assumptions = ExecutionAssumptions(
        taker_fee_bps=Decimal("0"),
        base_slippage_bps=Decimal("0"),
        random_slippage_bps=Decimal("0"),
        funding_bps_per_8h=Decimal("0"),
        pessimistic_intrabar=True,
    )
    candles = [
        candle(0, "100", "100", "100"),
        candle(1, "104", "94", "101"),
    ]

    result = simulate_realistic_trade(
        candles,
        0,
        Direction.LONG,
        entry=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        quantity=Decimal("1"),
        risk_amount=Decimal("5"),
        max_bars=1,
        assumptions=assumptions,
    )

    assert result.reason == "stop_loss"
    assert result.net_pnl == Decimal("-5")
    assert result.r_multiple == Decimal("-1")


def test_signed_funding_can_credit_shorts() -> None:
    assumptions = ExecutionAssumptions(
        taker_fee_bps=Decimal("0"),
        base_slippage_bps=Decimal("0"),
        random_slippage_bps=Decimal("0"),
        funding_bps_per_8h=Decimal("0"),
        funding_rate_per_8h=Decimal("0.0001"),
    )
    candles = [
        candle(0, "100", "100", "100"),
        candle(8, "101", "94", "95"),
    ]

    result = simulate_realistic_trade(
        candles,
        0,
        Direction.SHORT,
        entry=Decimal("100"),
        stop_loss=Decimal("105"),
        take_profit=Decimal("90"),
        quantity=Decimal("1"),
        risk_amount=Decimal("5"),
        max_bars=1,
        assumptions=assumptions,
        partial_targets=[{"name": "TP", "price": Decimal("95"), "quantity": Decimal("1")}],
    )

    assert result.reason == "take_profit"
    assert result.gross_pnl == Decimal("5")
    assert result.funding_cost < 0
    assert result.net_pnl > result.gross_pnl


def test_fallback_funding_bps_can_credit_shorts() -> None:
    assumptions = ExecutionAssumptions(
        taker_fee_bps=Decimal("0"),
        base_slippage_bps=Decimal("0"),
        random_slippage_bps=Decimal("0"),
        funding_bps_per_8h=Decimal("1"),
    )
    candles = [
        candle(0, "100", "100", "100"),
        candle(8, "101", "94", "95"),
    ]

    result = simulate_realistic_trade(
        candles,
        0,
        Direction.SHORT,
        entry=Decimal("100"),
        stop_loss=Decimal("105"),
        take_profit=Decimal("90"),
        quantity=Decimal("1"),
        risk_amount=Decimal("5"),
        max_bars=1,
        assumptions=assumptions,
        partial_targets=[{"name": "TP", "price": Decimal("95"), "quantity": Decimal("1")}],
    )

    assert result.funding_cost == Decimal("-0.01125")
    assert result.net_pnl == Decimal("5.01125")


def test_timeout_after_partial_target_closes_runner_at_timeout_candle_close() -> None:
    assumptions = ExecutionAssumptions(
        taker_fee_bps=Decimal("0"),
        base_slippage_bps=Decimal("0"),
        random_slippage_bps=Decimal("0"),
        funding_bps_per_8h=Decimal("0"),
    )
    candles = [
        candle(0, "100", "100", "100"),
        candle(1, "104", "99", "103"),
        candle(2, "104", "100", "101"),
        candle(3, "102", "96", "97"),
    ]

    result = simulate_realistic_trade(
        candles,
        0,
        Direction.LONG,
        entry=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("120"),
        quantity=Decimal("2"),
        risk_amount=Decimal("10"),
        max_bars=3,
        assumptions=assumptions,
        partial_targets=[
            {"name": "TP1", "price": Decimal("103"), "quantity": Decimal("1")},
            {"name": "TP2", "price": Decimal("120"), "quantity": Decimal("1")},
        ],
    )

    assert result.reason == "timeout"
    assert result.exit_index == 3
    assert result.exit_price == Decimal("97")
    assert result.filled_targets == ("TP1",)
    assert result.net_pnl == Decimal("0")


def test_quantity_estimate_includes_round_turn_costs() -> None:
    assumptions = ExecutionAssumptions(
        taker_fee_bps=Decimal("4"),
        base_slippage_bps=Decimal("5"),
        random_slippage_bps=Decimal("0"),
        funding_bps_per_8h=Decimal("1"),
    )

    qty = estimate_quantity_for_risk(
        entry=Decimal("100"),
        stop_loss=Decimal("95"),
        risk_amount=Decimal("10"),
        assumptions=assumptions,
    )

    assert qty < Decimal("2")
    assert qty > Decimal("1.9")
