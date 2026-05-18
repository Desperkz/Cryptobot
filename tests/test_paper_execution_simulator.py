from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import paper_monitor_v2 as monitor


def test_execution_pnl_subtracts_fee_slippage_and_funding(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "TAKER_FEE_BPS", Decimal("4.0"))
    monkeypatch.setattr(monitor, "SLIPPAGE_BPS", Decimal("10.0"))
    monkeypatch.setattr(monitor, "FUNDING_BPS_PER_8H", Decimal("1.0"))

    execution = monitor._execution_pnl(
        "LONG",
        Decimal("100"),
        Decimal("110"),
        Decimal("1"),
        opened_at=datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc),
        closed_at=datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc),
    )

    assert execution.gross_pnl == Decimal("10")
    assert execution.slippage_cost == Decimal("0.110")
    assert execution.fees == Decimal("0.0839560")
    assert execution.funding_cost == Decimal("0.010")
    assert execution.net_pnl == Decimal("9.7960440")


def test_pessimistic_intrabar_prefers_stop_when_tp_and_sl_are_inside_same_candle(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "PESSIMISTIC_INTRABAR", True)
    snapshot = monitor.MarketSnapshot(price=Decimal("108"), high=Decimal("111"), low=Decimal("94"))

    event = monitor._next_exit_event(
        "LONG",
        snapshot,
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        partial_targets=[{"name": "TP1", "price": "106"}],
        filled_targets=set(),
    )

    assert event == ("stop_loss", Decimal("95"), None)


def test_next_exit_event_hits_first_unfilled_partial_before_final_take_profit(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "PESSIMISTIC_INTRABAR", True)
    snapshot = monitor.MarketSnapshot(price=Decimal("108"), high=Decimal("111"), low=Decimal("101"))

    reason, price, target = monitor._next_exit_event(
        "LONG",
        snapshot,
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        partial_targets=[
            {"name": "TP1", "price": "106"},
            {"name": "TP2", "price": "110"},
        ],
        filled_targets=set(),
    )

    assert reason == "partial_take_profit"
    assert price == Decimal("106")
    assert target["name"] == "TP1"


def test_breakeven_and_trailing_move_stop_in_profitable_direction(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "BREAKEVEN_OFFSET_BPS", Decimal("2"))
    monkeypatch.setattr(monitor, "TRAILING_CALLBACK_RATE_PCT", Decimal("1"))

    assert monitor._breakeven_price("LONG", Decimal("100"), {}) == Decimal("100.02")
    assert monitor._breakeven_price("SHORT", Decimal("100"), {}) == Decimal("99.98")

    stop, changed = monitor._apply_trailing_stop(
        "LONG",
        monitor.MarketSnapshot(price=Decimal("110"), high=Decimal("112"), low=Decimal("109")),
        Decimal("100"),
        {"trailing_active": True},
    )

    assert changed is True
    assert stop == Decimal("110.88")
