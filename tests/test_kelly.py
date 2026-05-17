from __future__ import annotations

from decimal import Decimal

from trading_bot.config import RiskConfig
from trading_bot.risk_manager import KellyRiskSizer


def cfg() -> RiskConfig:
    return RiskConfig(
        risk_per_trade_pct=Decimal("0.02"),
        aggressive_risk_threshold_pct=Decimal("0.05"),
        max_concurrent_positions=4,
        max_leverage=5,
        default_leverage=3,
        max_daily_loss_pct=Decimal("0.06"),
        cooldown_after_losses=2,
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
        taker_fee_bps=Decimal("4"),
        slippage_bps=Decimal("5"),
        funding_buffer_bps=Decimal("1"),
        liquidation_buffer_pct=Decimal("0.03"),
        require_liquidation_check_in_live=True,
        adaptive_kelly_enabled=True,
        kelly_lookback_trades=50,
        kelly_fraction=Decimal("0.5"),
        kelly_min_risk_pct=Decimal("0.005"),
        kelly_max_risk_pct=Decimal("0.03"),
        correlation_groups={},
    )


def test_kelly_is_capped() -> None:
    trades = [{"r_multiple": "2"} for _ in range(30)] + [{"r_multiple": "-1"} for _ in range(10)]
    risk = KellyRiskSizer(cfg()).risk_pct(trades)

    assert risk == Decimal("0.03")


def test_kelly_reduces_to_min_when_edge_is_bad() -> None:
    trades = [{"r_multiple": "-1"} for _ in range(20)] + [{"r_multiple": "0.5"} for _ in range(5)]
    risk = KellyRiskSizer(cfg()).risk_pct(trades)

    assert risk == Decimal("0.005")
