from __future__ import annotations

from decimal import Decimal

from trading_bot.config import PartialTakeProfitConfig, TradeManagementConfig
from trading_bot.models import Direction, Signal, SymbolFilters, TradingStyle
from trading_bot.trade_manager.exit_plan import ExitPlanBuilder


def trade_management_config() -> TradeManagementConfig:
    return TradeManagementConfig(
        user_stream_required_for_live=True,
        user_stream_stale_after_sec=20,
        user_stream_reconnect_backoff_sec=5,
        rest_reconciliation_when_stale=True,
        partial_take_profits=[
            PartialTakeProfitConfig("TP1", Decimal("0.6"), Decimal("0.50"), move_stop_to_breakeven=True),
            PartialTakeProfitConfig("TP2", Decimal("1.1"), Decimal("0.50"), activate_trailing=True),
        ],
        breakeven_offset_bps=Decimal("2"),
        trailing_enabled=True,
        trailing_activation_reward_risk=Decimal("0.8"),
        trailing_callback_rate_pct={"INTRADAY": Decimal("0.4")},
        trailing_client_side=False,
        strategy_exit_profiles={
            "SQUEEZE_BREAKOUT": [
                PartialTakeProfitConfig("TP1", Decimal("0.8"), Decimal("0.35"), move_stop_to_breakeven=True),
                PartialTakeProfitConfig("TP2", Decimal("1.4"), Decimal("0.35"), activate_trailing=True),
                PartialTakeProfitConfig("RUNNER", Decimal("1.8"), Decimal("0.30"), activate_trailing=True),
            ],
            "MOMENTUM_CONTINUATION": [
                PartialTakeProfitConfig("TP1", Decimal("0.9"), Decimal("0.30"), move_stop_to_breakeven=True),
                PartialTakeProfitConfig("TP2", Decimal("1.4"), Decimal("0.35"), activate_trailing=True),
                PartialTakeProfitConfig("RUNNER", Decimal("2.0"), Decimal("0.35"), activate_trailing=True),
            ],
        },
    )


def filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def signal(strategy: str, take_profit: Decimal = Decimal("109")) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=take_profit,
        confidence=Decimal("0.8"),
        reason="test",
        metadata={"strategy": strategy},
    )


def test_squeeze_uses_runner_friendly_exit_profile() -> None:
    builder = ExitPlanBuilder(trade_management_config())

    targets = builder.build_targets(signal("SQUEEZE_BREAKOUT"), Decimal("10"), filters())
    protection = builder.build_protection(signal("SQUEEZE_BREAKOUT"), TradingStyle.INTRADAY, filters())

    assert [target.name for target in targets] == ["TP1", "TP2", "RUNNER"]
    assert [target.quantity for target in targets] == [Decimal("3.500"), Decimal("3.500"), Decimal("3.000")]
    assert [target.reward_risk for target in targets] == [Decimal("0.8"), Decimal("1.4"), Decimal("1.8")]
    assert protection.breakeven_after_target == "TP1"


def test_unknown_strategy_falls_back_to_default_exit_profile() -> None:
    builder = ExitPlanBuilder(trade_management_config())

    targets = builder.build_targets(signal("UNKNOWN"), Decimal("10"), filters())

    assert [target.name for target in targets] == ["TP1", "TP2"]
    assert [target.quantity for target in targets] == [Decimal("5.000"), Decimal("5.000")]
    assert [target.reward_risk for target in targets] == [Decimal("0.6"), Decimal("1.1")]


def test_profile_targets_are_capped_at_signal_take_profit_rr() -> None:
    builder = ExitPlanBuilder(trade_management_config())

    targets = builder.build_targets(signal("MOMENTUM_CONTINUATION", take_profit=Decimal("107.5")), Decimal("10"), filters())

    assert [target.name for target in targets] == ["TP1", "TP2", "RUNNER"]
    assert targets[-1].reward_risk == Decimal("1.5")
    assert targets[-1].price == Decimal("107.5")
