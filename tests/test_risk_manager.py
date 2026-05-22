from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_bot.config import PartialTakeProfitConfig, RiskConfig, TradeManagementConfig
from trading_bot.models import Direction, Position, Signal, SymbolFilters, TradingStyle
from trading_bot.risk_manager import RiskError, RiskManager


def risk_config() -> RiskConfig:
    return RiskConfig(
        risk_per_trade_pct=Decimal("0.02"),
        aggressive_risk_threshold_pct=Decimal("0.05"),
        max_concurrent_positions=4,
        max_leverage=5,
        default_leverage=3,
        max_daily_loss_pct=Decimal("0.05"),
        cooldown_after_losses=3,
        cooldown_minutes=120,
        symbol_cooldown_after_loss_minutes=120,
        strategy_reentry_cooldown_minutes=45,
        scale_in_enabled=False,
        max_scale_ins_per_symbol_strategy=2,
        scale_in_risk_multiplier=Decimal("0.50"),
        scale_in_min_unrealized_r=Decimal("0.25"),
        scale_in_independent_signal_minutes=60,
        trade_cluster_window_minutes=60,
        max_portfolio_risk_pct=Decimal("0.15"),
        max_correlated_group_risk_pct=Decimal("0.10"),
        max_margin_usage_pct=Decimal("0.60"),
        min_reward_risk=Decimal("1.2"),
        taker_fee_bps=Decimal("4.0"),
        slippage_bps=Decimal("5.0"),
        funding_buffer_bps=Decimal("1.0"),
        liquidation_buffer_pct=Decimal("0.03"),
        require_liquidation_check_in_live=True,
        adaptive_kelly_enabled=True,
        kelly_lookback_trades=50,
        kelly_fraction=Decimal("0.5"),
        kelly_min_risk_pct=Decimal("0.005"),
        kelly_max_risk_pct=Decimal("0.03"),
        correlation_groups={},
    )


def trade_management_config() -> TradeManagementConfig:
    return TradeManagementConfig(
        user_stream_required_for_live=True,
        user_stream_stale_after_sec=20,
        user_stream_reconnect_backoff_sec=5,
        rest_reconciliation_when_stale=True,
        partial_take_profits=[
            PartialTakeProfitConfig("TP1", Decimal("1"), Decimal("0.4"), move_stop_to_breakeven=True),
            PartialTakeProfitConfig("TP2", Decimal("1.8"), Decimal("0.35"), activate_trailing=True),
            PartialTakeProfitConfig("TP3", Decimal("2.8"), Decimal("0.25")),
        ],
        breakeven_offset_bps=Decimal("2"),
        trailing_enabled=True,
        trailing_activation_reward_risk=Decimal("1.8"),
        trailing_callback_rate_pct={"INTRADAY": Decimal("0.6")},
        trailing_client_side=False,
    )


def filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def long_signal(stop_loss: Decimal | None = Decimal("95"), take_profit: Decimal | None = Decimal("110")) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=Decimal("0.7"),
        reason="test",
    )


def test_position_size_uses_stop_distance_and_caps_risk() -> None:
    manager = RiskManager(risk_config(), trade_management_config())
    plan = manager.calculate_plan(long_signal(), Decimal("1000"), filters(), leverage=5)

    assert plan.quantity > 0
    assert plan.notional <= Decimal("5000")
    assert plan.risk_amount <= Decimal("100.10")
    assert len(plan.partial_take_profits) == 3
    assert sum(target.quantity for target in plan.partial_take_profits) == plan.quantity
    assert plan.protection is not None
    assert plan.signal_metadata["exit_profile_signature"] == "TP1:1R@0.4/BE|TP2:1.8R@0.35/TR|TP3:2R@0.25"
    assert Decimal(plan.signal_metadata["first_target_net_reward_risk"]) > Decimal("0")


def test_signed_funding_impact_blocks_expensive_adverse_direction() -> None:
    manager = RiskManager(risk_config(), trade_management_config())
    signal = replace(long_signal(), metadata={"funding_rate": "0.0010"})

    with pytest.raises(RiskError, match="Estimated funding impact 10.00 bps exceeds maximum"):
        manager.calculate_plan(signal, Decimal("1000"), filters(), leverage=3)


def test_signed_funding_credit_does_not_charge_fixed_buffer() -> None:
    manager = RiskManager(risk_config(), trade_management_config())
    signal = Signal(
        symbol="BTCUSDT",
        direction=Direction.SHORT,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("105"),
        take_profit=Decimal("90"),
        confidence=Decimal("0.7"),
        reason="favorable funding",
        metadata={"funding_rate": "0.0010"},
    )

    plan = manager.calculate_plan(signal, Decimal("1000"), filters(), leverage=3)

    assert plan.signal_metadata["risk_funding_impact_bps"] == "0"
    assert plan.signal_metadata["risk_signed_funding_impact_bps"] == "-10.0000"
    assert plan.signal_metadata["risk_funding_impact_source"] == "signed_estimate"
    assert any("funding is favorable" in item for item in plan.warnings)


def test_missing_funding_rate_keeps_fallback_buffer() -> None:
    manager = RiskManager(risk_config(), trade_management_config())

    plan = manager.calculate_plan(long_signal(), Decimal("1000"), filters(), leverage=3)

    assert plan.signal_metadata["risk_funding_impact_bps"] == "1.0"
    assert plan.signal_metadata["risk_funding_impact_source"] == "fallback_buffer"


def test_position_size_is_reduced_to_margin_usage_limit_instead_of_rejected() -> None:
    manager = RiskManager(risk_config(), trade_management_config())
    signal = Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("1000"),
        stop_loss=Decimal("998"),
        take_profit=Decimal("1004"),
        confidence=Decimal("0.8"),
        reason="tight stop high notional",
    )

    plan = manager.calculate_plan(signal, Decimal("1000"), filters(), leverage=3)

    assert plan.initial_margin <= Decimal("600")
    assert plan.notional <= Decimal("1800")
    assert plan.risk_amount < Decimal("20")
    assert any("max margin usage" in item for item in plan.warnings)


def test_first_partial_target_must_have_enough_net_reward_after_costs() -> None:
    trade_management = replace(
        trade_management_config(),
        partial_take_profits=[
            PartialTakeProfitConfig("TP1", Decimal("0.20"), Decimal("0.40"), move_stop_to_breakeven=True),
            PartialTakeProfitConfig("TP2", Decimal("1.8"), Decimal("0.35"), activate_trailing=True),
            PartialTakeProfitConfig("TP3", Decimal("2.8"), Decimal("0.25")),
        ],
        min_first_target_net_reward_risk=Decimal("0.30"),
    )
    manager = RiskManager(risk_config(), trade_management)

    with pytest.raises(RiskError, match="First partial TP is too small"):
        manager.calculate_plan(long_signal(), Decimal("1000"), filters(), leverage=3)


def test_stop_loss_and_take_profit_are_mandatory() -> None:
    manager = RiskManager(risk_config(), trade_management_config())

    with pytest.raises(RiskError, match="Stop-loss is mandatory"):
        manager.calculate_plan(long_signal(stop_loss=None), Decimal("1000"), filters())

    with pytest.raises(RiskError, match="Take-profit is mandatory"):
        manager.calculate_plan(long_signal(take_profit=None), Decimal("1000"), filters())


def test_same_symbol_position_blocks_new_trade_but_other_symbol_is_allowed() -> None:
    manager = RiskManager(risk_config(), trade_management_config())
    other_symbol = [Position("ETHUSDT", Direction.LONG, Decimal("0.01"), Decimal("1000"), stop_loss=Decimal("950"))]

    plan = manager.calculate_plan(long_signal(), Decimal("1000"), filters(), leverage=5, active_positions=other_symbol)
    assert plan.symbol == "BTCUSDT"

    active = [Position("ETHUSDT", Direction.LONG, Decimal("2"), Decimal("1000"))]

    with pytest.raises(RiskError, match="max portfolio risk|correlated group|margin"):
        manager.calculate_plan(long_signal(), Decimal("1000"), filters(), leverage=5, active_positions=active)

    same_symbol = [Position("BTCUSDT", Direction.LONG, Decimal("0.01"), Decimal("100"), stop_loss=Decimal("95"))]
    with pytest.raises(RiskError, match="active BTCUSDT position"):
        manager.calculate_plan(long_signal(), Decimal("1000"), filters(), leverage=5, active_positions=same_symbol)


def test_live_mode_requires_liquidation_check() -> None:
    manager = RiskManager(risk_config(), trade_management_config())

    plan = manager.calculate_plan(long_signal(), Decimal("1000"), filters(), live_mode=True)

    assert any("Liquidation check used a conservative pre-trade estimate" in item for item in plan.warnings)


def test_stop_too_close_to_liquidation_is_rejected() -> None:
    manager = RiskManager(risk_config(), trade_management_config())

    with pytest.raises(RiskError, match="too close to liquidation"):
        manager.calculate_plan(
            long_signal(stop_loss=Decimal("82"), take_profit=Decimal("130")),
            Decimal("1000"),
            filters(),
            liquidation_price=Decimal("80"),
            live_mode=True,
        )
