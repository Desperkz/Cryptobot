"""P8: тесты режима order_flow_entry_gate_mode="observe" и контекстного гейта.

Эмпирическая база (экспорт paper/shadow 2026-07-14..09-14, 200 независимых
SQZ-сигналов): направленные order-flow гейты предсказывали исход с обратным
знаком (aligned -0.009R против +0.339R у остальных, p=0.023), тогда как
структурные проверки сохраняли знак. Эти тесты фиксируют, что observe снимает
именно направленные гейты и ни один структурный.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.bot import (
    _is_strong_clean_squeeze_release,
    _order_flow_entry_rejection_reason,
    _squeeze_context_gate_rejection,
)
from trading_bot.config import ConfigError, load_config
from trading_bot.models import Direction, Signal, TradingStyle


class _StrategyStub:
    def __init__(
        self,
        *,
        mode: str = "observe",
        context_gate: bool = True,
        blocked: list[str] | None = None,
    ) -> None:
        self.order_flow_entry_gate_mode = mode
        self.order_flow_hostile_score_floor = Decimal("0.70")
        self.order_flow_mixed_score_floor = Decimal("0.45")
        self.squeeze_context_gate_enabled = context_gate
        self.squeeze_context_gate_require_4h_squeeze_or_trend = True
        self.squeeze_context_gate_blocked_regimes = list(blocked or ["RANGE"])


def _signal(**metadata) -> Signal:
    base = {
        "strategy": "SQUEEZE_BREAKOUT",
        "regime": "TREND_UP",
        "squeeze_bars_4h": 3,
        "squeeze_retest_confirmed": True,
        "relative_strength": {"alignment": "aligned", "score": "0.80"},
        "order_flow": {
            "alignment": "aligned",
            "score": "0.60",
            "reasons": ["structure_break_aligned"],
            "risk_flags": [],
        },
    }
    base.update(metadata)
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("112"),
        confidence=Decimal("0.8"),
        reason="test",
        timeframe="15m",
        metadata=base,
    )


def _with_order_flow(signal: Signal, **changes) -> Signal:
    order_flow = dict(signal.metadata["order_flow"])
    order_flow.update(changes)
    signal.metadata["order_flow"] = order_flow
    return signal


def test_observe_admits_order_flow_against_breakout() -> None:
    """alignment=against давал +0.307R и не должен блокировать вход."""
    signal = _with_order_flow(_signal(), alignment="against")
    assert _order_flow_entry_rejection_reason(signal, _StrategyStub(mode="strict")) is not None
    assert _order_flow_entry_rejection_reason(signal, _StrategyStub()) is None


def test_observe_admits_weak_mixed_score() -> None:
    signal = _with_order_flow(_signal(), alignment="mixed", score="0.10")
    assert _order_flow_entry_rejection_reason(signal, _StrategyStub(mode="strict")) is not None
    assert _order_flow_entry_rejection_reason(signal, _StrategyStub()) is None


def test_observe_admits_directional_hostile_flags() -> None:
    """taker/delta/book against — знак обратный, гейт снимается."""
    signal = _with_order_flow(
        _signal(),
        alignment="mixed",
        score="0.50",
        risk_flags=["taker_flow_against", "book_imbalance_against"],
    )
    assert _order_flow_entry_rejection_reason(signal, _StrategyStub()) is None


@pytest.mark.parametrize(
    "flag",
    ["liquidation_cascade", "structure_break_against", "adverse_liquidity_nearby"],
)
def test_observe_keeps_structural_flags_hard(flag: str) -> None:
    """Структурные флаги остаются жёсткими в обоих режимах."""
    signal = _with_order_flow(_signal(), score="0.50", risk_flags=[flag])
    rejection = _order_flow_entry_rejection_reason(signal, _StrategyStub())
    assert rejection is not None and rejection[0] == "ORDER_FLOW"


def test_observe_keeps_absorption_against_hard() -> None:
    """absorption_against давал -0.405R к матожиданию — блокировка сохраняется."""
    signal = _with_order_flow(_signal(), risk_flags=["absorption_against"])
    rejection = _order_flow_entry_rejection_reason(signal, _StrategyStub())
    assert rejection is not None and rejection[0] == "ORDER_FLOW"


def test_observe_keeps_relative_strength_hard() -> None:
    """relative strength сохранял знак (+0.644R) и остаётся обязательной."""
    signal = _signal(relative_strength={"alignment": "against", "score": "0.10"})
    rejection = _order_flow_entry_rejection_reason(signal, _StrategyStub())
    assert rejection is not None and rejection[0] == "RELATIVE_STRENGTH"


def test_context_gate_blocks_range_without_4h_squeeze() -> None:
    signal = _signal(regime="RANGE", squeeze_bars_4h=0)
    rejection = _squeeze_context_gate_rejection(signal, _StrategyStub())
    assert rejection is not None and rejection[0] == "SQZ_CONTEXT"


def test_context_gate_admits_range_with_4h_squeeze() -> None:
    """Сжатие на 4h — самостоятельное подтверждение (+0.694R против +0.027R)."""
    signal = _signal(regime="RANGE", squeeze_bars_4h=2)
    assert _squeeze_context_gate_rejection(signal, _StrategyStub()) is None


def test_context_gate_admits_trend_without_4h_squeeze() -> None:
    signal = _signal(regime="TREND_UP", squeeze_bars_4h=0)
    assert _squeeze_context_gate_rejection(signal, _StrategyStub()) is None


def test_context_gate_is_off_when_disabled() -> None:
    signal = _signal(regime="RANGE", squeeze_bars_4h=0)
    assert _squeeze_context_gate_rejection(signal, _StrategyStub(context_gate=False)) is None


def test_strong_release_ignores_alignment_only_in_observe() -> None:
    """Исключение из retest-гейта больше не опирается на направленный OF."""
    signal = _signal(
        squeeze_retest_confirmed=False,
        squeeze_state="release",
        squeeze_entry_timing="release_followthrough",
        breakout_atr="1.80",
    )
    kwargs = dict(alignment="against", score=Decimal("0.20"), risk_flags=set())
    assert _is_strong_clean_squeeze_release(signal, observe_mode=True, **kwargs) is True
    assert _is_strong_clean_squeeze_release(signal, observe_mode=False, **kwargs) is False


def test_strong_release_still_requires_a_strong_release() -> None:
    signal = _signal(
        squeeze_retest_confirmed=False,
        squeeze_state="release",
        squeeze_entry_timing="release_followthrough",
        breakout_atr="0.40",
    )
    assert (
        _is_strong_clean_squeeze_release(
            signal,
            alignment="aligned",
            score=Decimal("0.90"),
            risk_flags=set(),
            observe_mode=True,
        )
        is False
    )


def test_strong_release_in_observe_still_blocks_structural_flags() -> None:
    signal = _signal(
        squeeze_retest_confirmed=False,
        squeeze_state="release",
        squeeze_entry_timing="release_followthrough",
        breakout_atr="1.80",
    )
    assert (
        _is_strong_clean_squeeze_release(
            signal,
            alignment="mixed",
            score=Decimal("0.50"),
            risk_flags={"liquidation_cascade"},
            observe_mode=True,
        )
        is False
    )


def test_observe_mode_requires_context_gate_in_config() -> None:
    """observe без контекстного гейта впустил бы весь поток RANGE-пробоев."""
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml", root / ".env.example")
    object.__setattr__(cfg.strategy, "squeeze_context_gate_enabled", False)
    with pytest.raises(ConfigError, match="squeeze_context_gate_enabled"):
        cfg.validate()


def test_runtime_config_runs_observe_mode_with_context_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml", root / ".env.example")
    assert cfg.strategy.order_flow_entry_gate_mode == "observe"
    assert cfg.strategy.squeeze_context_gate_enabled is True
    assert cfg.strategy.squeeze_context_gate_blocked_regimes == ["RANGE"]
    assert cfg.strategy.shadow_conditional_neutralize_order_flow is True
    cfg.validate()
