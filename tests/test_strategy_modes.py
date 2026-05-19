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


def signal(strategy: str, confidence: str = "0.7") -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
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
    ]
    assert cfg.strategy.execution_strategies(TradingMode.MAINNET_LIVE) == []
    assert cfg.strategy.shadow_strategies() == [
        "SQUEEZE_BREAKOUT_DYNAMIC",
        "TREND_PULLBACK",
        "LIQUIDITY_SWEEP_REVERSAL",
        "VWAP_REVERSION",
        "VWAP_REVERSION_WATCH",
        "MOMENTUM_CONTINUATION",
        "RANGE_GRID",
        "TREND_FOLLOWING",
    ]


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
