from __future__ import annotations

from decimal import Decimal

from trading_bot.config import RiskConfig
from trading_bot.models import Direction, Signal, TradingStyle
from trading_bot.risk_manager.dynamic_sizing import dynamic_position_sizing


def risk_config(**overrides) -> RiskConfig:
    values = dict(
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
        min_reward_risk=Decimal("1.0"),
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
    values.update(overrides)
    return RiskConfig(**values)


def signal(strategy: str, confidence: str, order_flow: dict | None = None) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        confidence=Decimal(confidence),
        reason=f"{strategy} test",
        metadata={"strategy": strategy, "order_flow": order_flow or {}},
    )


def test_sqz_champion_can_reduce_but_not_exceed_champion_cap() -> None:
    decision = dynamic_position_sizing(
        signal=signal(
            "SQUEEZE_BREAKOUT",
            "0.93",
            {"alignment": "aligned", "score": 0.9, "risk_flags": []},
        ),
        config=risk_config(),
        base_risk_pct=Decimal("0.02"),
    )

    assert decision.risk_pct == Decimal("0.020")
    assert decision.leverage == 5
    assert decision.cap_risk_pct == Decimal("0.020")


def test_sqz_dynamic_challenger_can_size_above_champion_in_shadow() -> None:
    decision = dynamic_position_sizing(
        signal=signal(
            "SQUEEZE_BREAKOUT_DYNAMIC",
            "0.93",
            {"alignment": "aligned", "score": 0.9, "risk_flags": []},
        ),
        config=risk_config(dynamic_sizing_shadow_max_risk_pct=Decimal("0.025")),
        base_risk_pct=Decimal("0.02"),
        strategy_mode="shadow",
    )

    assert decision.risk_pct == Decimal("0.025")
    assert decision.leverage == 5
    assert decision.cap_risk_pct == Decimal("0.025")


def test_hostile_order_flow_cuts_risk_and_leverage() -> None:
    decision = dynamic_position_sizing(
        signal=signal(
            "MEAN_REVERSION",
            "0.75",
            {"alignment": "against", "score": 0.25, "risk_flags": ["taker_flow_against"]},
        ),
        config=risk_config(),
        base_risk_pct=Decimal("0.02"),
    )

    assert decision.risk_pct < Decimal("0.008")
    assert decision.leverage == 2
    assert "order_flow=hostile" in decision.reasons


def test_disabled_dynamic_sizing_returns_base_risk() -> None:
    decision = dynamic_position_sizing(
        signal=signal("RANGE_GRID", "0.55"),
        config=risk_config(dynamic_sizing_enabled=False),
        base_risk_pct=Decimal("0.02"),
    )

    assert decision.risk_pct == Decimal("0.02")
    assert decision.leverage == 3
    assert decision.reasons == ("dynamic_sizing_disabled",)
