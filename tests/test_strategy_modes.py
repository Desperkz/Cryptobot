from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trading_bot.config import StrategyConfig, load_config
from trading_bot.models import Direction, Signal, TradingMode, TradingStyle
from trading_bot.strategy_engine.router import StrategyRouter


def strategy_config(
    *,
    enabled: list[str],
    modes: dict[str, str] | None = None,
) -> StrategyConfig:
    return StrategyConfig(
        ema_fast=20,
        ema_mid=50,
        ema_slow=200,
        rsi_period=14,
        atr_period=14,
        volume_lookback=20,
        min_volume_ratio=Decimal("1.2"),
        min_atr_pct=Decimal("0.15"),
        max_atr_pct=Decimal("8.0"),
        stop_atr_multiplier={"SCALPING": Decimal("1.2"), "INTRADAY": Decimal("1.8")},
        take_profit_rr={"SCALPING": Decimal("1.4"), "INTRADAY": Decimal("1.8")},
        use_funding_filter=True,
        max_abs_funding_rate=Decimal("0.0008"),
        enabled_strategies=enabled,
        strategy_modes=modes or {},
    )


def signal(strategy: str, confidence: str = "0.7", direction: Direction = Direction.LONG) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=direction,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        confidence=Decimal(confidence),
        reason=f"{strategy} test",
        metadata={"strategy": strategy},
    )


class StubStrategy:
    def __init__(self, item: Signal | None) -> None:
        self.item = item

    def generate(self, *_args, **_kwargs) -> Signal | None:
        return self.item


class DiagnosticStubStrategy(StubStrategy):
    def evaluate(self, *_args, **_kwargs) -> tuple[Signal | None, dict[str, str]]:
        return self.item, {
            "strategy": "TREND_FOLLOWING",
            "symbol": "BTCUSDT",
            "decision": "NO_SIGNAL",
            "block_reason": "weak_volume",
        }


def test_strategy_modes_split_execution_shadow_and_live_promotion() -> None:
    cfg = strategy_config(
        enabled=["SQUEEZE_BREAKOUT", "MEAN_REVERSION"],
        modes={
            "SQUEEZE_BREAKOUT": "paper",
            "MEAN_REVERSION": "shadow",
            "TREND_PULLBACK": "shadow",
            "TREND_FOLLOWING": "disabled",
        },
    )

    assert cfg.execution_strategies(TradingMode.PAPER_TRADING) == ["SQUEEZE_BREAKOUT"]
    assert cfg.execution_strategies(TradingMode.TESTNET_LIVE) == ["SQUEEZE_BREAKOUT"]
    assert cfg.execution_strategies(TradingMode.MAINNET_LIVE) == []
    assert cfg.shadow_strategies() == ["MEAN_REVERSION", "TREND_PULLBACK"]
    assert cfg.mode_summary()["TREND_FOLLOWING"] == "disabled"

    promoted = strategy_config(
        enabled=["SQUEEZE_BREAKOUT"],
        modes={"SQUEEZE_BREAKOUT": "live"},
    )
    assert promoted.execution_strategies(TradingMode.MAINNET_LIVE) == ["SQUEEZE_BREAKOUT"]


def test_enabled_strategies_default_to_paper_for_backward_compatibility() -> None:
    cfg = strategy_config(enabled=["SQUEEZE_BREAKOUT"], modes={})

    assert cfg.mode_for_strategy("SQUEEZE_BREAKOUT") == "paper"
    assert cfg.execution_strategies(TradingMode.PAPER_TRADING) == ["SQUEEZE_BREAKOUT"]
    assert cfg.shadow_strategies() == []


def test_runtime_config_executes_mean_reversion_in_paper_only() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml", root / ".env.example")

    assert cfg.strategy.execution_strategies(TradingMode.PAPER_TRADING) == [
        "MEAN_REVERSION",
        "SQUEEZE_BREAKOUT",
        "SQUEEZE_BREAKOUT_OF_MEASURE",
    ]
    assert cfg.strategy.execution_strategies(TradingMode.MAINNET_LIVE) == []
    assert cfg.strategy.shadow_strategies() == [
        "SQZ_STRICT_CONTROL_SHADOW",
        "SQZ_OF_AGAINST_SHADOW",
        "SQZ_OF_HOSTILE_SHADOW",
        "SQZ_OF_ABSORPTION_SHADOW",
        "SQZ_RS_NEUTRAL_SHADOW",
        "SQZ_NO_RETEST_SHADOW",
        "SQUEEZE_BREAKOUT_DYNAMIC",
        "SQUEEZE_BREAKOUT_DYNAMIC_NEUTRAL_RS",
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        "TREND_PULLBACK",
        "LIQUIDITY_SWEEP_REVERSAL",
        "VWAP_REVERSION",
        "VWAP_REVERSION_WATCH",
        "MOMENTUM_CONTINUATION",
        "RANGE_GRID",
        "TREND_FOLLOWING",
    ]
    assert cfg.strategy.shadow_gate_counterfactual_enabled is False
    assert cfg.strategy.shadow_parallel_lab_enabled is True
    assert cfg.strategy.shadow_parallel_lab_cohort == "2026-08-14-parallel-lab"
    assert "STRICT" in cfg.strategy.shadow_parallel_lab_arms
    assert "OF_HOSTILE+MISSING_OI" in cfg.strategy.shadow_parallel_lab_arms
    assert cfg.strategy.shadow_conditional_lab_enabled is True
    assert cfg.strategy.shadow_conditional_lab_cohort == "2026-08-25-conditional-v1"
    assert cfg.strategy.shadow_conditional_lab_risk_cap_pct == Decimal("0.0020")
    assert cfg.strategy.shadow_conditional_lab_mid_score == Decimal("50")
    assert cfg.strategy.shadow_conditional_lab_high_score == Decimal("70")
    assert cfg.strategy.shadow_conditional_lab_v2_enabled is True
    assert cfg.strategy.shadow_conditional_lab_v2_cohort == "2026-09-03-conditional-v2"
    assert cfg.strategy.shadow_conditional_lab_v2_risk_cap_pct == Decimal("0.0020")
    assert cfg.strategy.shadow_conditional_lab_v2_mid_score == Decimal("50")
    assert cfg.strategy.shadow_conditional_lab_v2_high_score == Decimal("70")


def test_router_records_shadow_candidates_separately_from_execution() -> None:
    router = StrategyRouter(
        trend=StubStrategy(None),
        mean_reversion=StubStrategy(signal("MEAN_REVERSION", "0.8")),
        squeeze_breakout=StubStrategy(signal("SQUEEZE_BREAKOUT", "0.7")),
        trend_pullback=StubStrategy(signal("TREND_PULLBACK", "0.75")),
        enabled_strategies=["SQUEEZE_BREAKOUT"],
        shadow_strategies=["MEAN_REVERSION", "TREND_PULLBACK"],
    )

    execution = router.generate("BTCUSDT", [], [], [], None)
    shadow = router.generate_shadow("BTCUSDT", [], [], [], None)

    assert execution is not None
    assert execution.metadata["strategy"] == "SQUEEZE_BREAKOUT"
    assert [item.metadata["strategy"] for item in shadow] == ["MEAN_REVERSION", "TREND_PULLBACK"]


def test_router_can_emit_sqz_dynamic_as_separate_shadow_challenger() -> None:
    router = StrategyRouter(
        trend=StubStrategy(None),
        mean_reversion=StubStrategy(None),
        squeeze_breakout=StubStrategy(signal("SQUEEZE_BREAKOUT", "0.9")),
        enabled_strategies=["SQUEEZE_BREAKOUT"],
        shadow_strategies=["SQUEEZE_BREAKOUT_DYNAMIC"],
    )

    execution = router.generate("BTCUSDT", [], [], [], None)
    shadow = router.generate_shadow("BTCUSDT", [], [], [], None)

    assert execution is not None
    assert execution.metadata["strategy"] == "SQUEEZE_BREAKOUT"
    assert len(shadow) == 1
    assert shadow[0].metadata["strategy"] == "SQUEEZE_BREAKOUT_DYNAMIC"
    assert shadow[0].metadata["parent_strategy"] == "SQUEEZE_BREAKOUT"


def test_router_can_emit_sqz_dynamic_updated_as_parallel_shadow_challenger() -> None:
    router = StrategyRouter(
        trend=StubStrategy(None),
        mean_reversion=StubStrategy(None),
        squeeze_breakout=StubStrategy(signal("SQUEEZE_BREAKOUT", "0.9")),
        enabled_strategies=["SQUEEZE_BREAKOUT"],
        shadow_strategies=["SQUEEZE_BREAKOUT_DYNAMIC", "SQUEEZE_BREAKOUT_DYNAMIC_UPD"],
    )

    shadow = router.generate_shadow("BTCUSDT", [], [], [], None)

    assert [item.metadata["strategy"] for item in shadow] == [
        "SQUEEZE_BREAKOUT_DYNAMIC",
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
    ]
    assert all(item.metadata["parent_strategy"] == "SQUEEZE_BREAKOUT" for item in shadow)


def test_router_records_trend_following_diagnostics_when_no_signal() -> None:
    router = StrategyRouter(
        trend=DiagnosticStubStrategy(None),
        mean_reversion=StubStrategy(None),
        enabled_strategies=[],
        shadow_strategies=["TREND_FOLLOWING"],
    )

    assert router.generate_shadow("BTCUSDT", [], [], [], None) == []

    diagnostics = router.drain_diagnostics()
    assert diagnostics == [
        {
            "strategy": "TREND_FOLLOWING",
            "symbol": "BTCUSDT",
            "decision": "NO_SIGNAL",
            "block_reason": "weak_volume",
        }
    ]


def test_router_resolves_opposite_direction_conflict_when_confidence_gap_is_clear() -> None:
    router = StrategyRouter(
        trend=StubStrategy(None),
        mean_reversion=StubStrategy(signal("MEAN_REVERSION", "0.70", Direction.SHORT)),
        squeeze_breakout=StubStrategy(signal("SQUEEZE_BREAKOUT", "0.86", Direction.LONG)),
        enabled_strategies=["MEAN_REVERSION", "SQUEEZE_BREAKOUT"],
    )

    selected = router.generate("BTCUSDT", [], [], [], None)

    assert selected is not None
    assert selected.metadata["strategy"] == "SQUEEZE_BREAKOUT"
    assert selected.metadata["direction_conflict_resolved"] is True
    assert selected.metadata["opposing_candidates"][0]["strategy"] == "MEAN_REVERSION"


def test_router_blocks_opposite_direction_conflict_when_confidence_gap_is_small() -> None:
    router = StrategyRouter(
        trend=StubStrategy(None),
        mean_reversion=StubStrategy(signal("MEAN_REVERSION", "0.80", Direction.SHORT)),
        squeeze_breakout=StubStrategy(signal("SQUEEZE_BREAKOUT", "0.84", Direction.LONG)),
        enabled_strategies=["MEAN_REVERSION", "SQUEEZE_BREAKOUT"],
    )

    assert router.generate("BTCUSDT", [], [], [], None) is None
