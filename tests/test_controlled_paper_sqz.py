from dataclasses import replace
from decimal import Decimal

from trading_bot.bot import (
    _cap_controlled_shadow_sizing,
    _controlled_shadow_sqz_dynamic_neutral_override,
    _cap_controlled_paper_sizing,
    _controlled_paper_risk_cap,
    _controlled_paper_sqz_override,
    _order_flow_entry_rejection_reason,
    _squeeze_order_flow_measurement_override,
    _shadow_candidate_context_rejection_reason,
    _shadow_execution_strategy,
    _trade_strategy,
)
from trading_bot.config import StrategyConfig
from trading_bot.models import Direction, Signal, TradingStyle
from trading_bot.risk_manager.dynamic_sizing import DynamicSizingDecision


def _config(
    *,
    enabled: bool = True,
    neutral_shadow_enabled: bool = False,
    measurement_enabled: bool = False,
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
        max_atr_pct=Decimal("8"),
        stop_atr_multiplier={"INTRADAY": Decimal("1.8")},
        take_profit_rr={"INTRADAY": Decimal("2.4")},
        use_funding_filter=True,
        max_abs_funding_rate=Decimal("0.0008"),
        squeeze_controlled_paper_enabled=enabled,
        squeeze_controlled_paper_min_order_flow_score=Decimal("0.65"),
        squeeze_controlled_paper_risk_cap_pct=Decimal("0.005"),
        order_flow_entry_gate_mode="measure",
        order_flow_mixed_score_floor=Decimal("0.45"),
        squeeze_order_flow_measurement_enabled=measurement_enabled,
        squeeze_order_flow_measurement_min_score=Decimal("0.30"),
        squeeze_order_flow_measurement_risk_cap_pct=Decimal("0.005"),
        squeeze_dynamic_neutral_shadow_enabled=neutral_shadow_enabled,
        squeeze_dynamic_neutral_shadow_min_order_flow_score=Decimal("0.65"),
        squeeze_dynamic_neutral_shadow_risk_cap_pct=Decimal("0.0025"),
    )


def _signal(
    *,
    relative_strength: str = "neutral",
    score: str = "0.72",
    flags: list[str] | None = None,
    retest: bool = True,
    reasons: list[str] | None = None,
) -> Signal:
    return Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("99"),
        take_profit=Decimal("103"),
        confidence=Decimal("0.80"),
        reason="test",
        metadata={
            "strategy": "SQUEEZE_BREAKOUT",
            "order_flow": {
                "alignment": "aligned",
                "score": score,
                "reasons": reasons or ["structure_break_aligned"],
                "risk_flags": flags or [],
            },
            "relative_strength": {"alignment": relative_strength},
            "squeeze_retest_confirmed": retest,
            "squeeze_state": "release",
            "squeeze_entry_timing": "release_followthrough",
            "breakout_atr": "1.20",
        },
    )


def test_controlled_paper_admits_only_neutral_relative_strength_after_clean_sqz_checks() -> None:
    signal = _signal()
    rejection = _order_flow_entry_rejection_reason(signal)

    admitted = _controlled_paper_sqz_override(signal, rejection, _config())

    assert admitted is not None
    assert admitted.metadata["controlled_paper"]["bucket"] == "sqz_relative_strength_neutral_v1"
    assert admitted.metadata["controlled_paper"]["risk_cap_pct"] == "0.005"


def test_controlled_paper_keeps_against_relative_strength_blocked() -> None:
    signal = _signal(relative_strength="against")

    assert _controlled_paper_sqz_override(signal, _order_flow_entry_rejection_reason(signal), _config()) is None


def test_controlled_paper_keeps_adverse_flow_blocked_even_at_high_score() -> None:
    signal = _signal(score="0.80", flags=["adverse_liquidity_nearby"])

    assert _controlled_paper_sqz_override(signal, _order_flow_entry_rejection_reason(signal), _config()) is None


def test_controlled_paper_keeps_weak_no_retest_release_blocked() -> None:
    signal = _signal(retest=False)

    assert _controlled_paper_sqz_override(signal, _order_flow_entry_rejection_reason(signal), _config()) is None


def test_controlled_paper_caps_risk_after_dynamic_sizing() -> None:
    sizing = DynamicSizingDecision(
        strategy="SQUEEZE_BREAKOUT",
        risk_pct=Decimal("0.008"),
        leverage=3,
        base_risk_pct=Decimal("0.005"),
        raw_risk_pct=Decimal("0.008"),
        multiplier=Decimal("1.6"),
        cap_risk_pct=Decimal("0.02"),
        reasons=("test",),
    )

    capped = _cap_controlled_paper_sizing(sizing, Decimal("0.005"))

    assert capped.risk_pct == Decimal("0.005")
    assert capped.cap_risk_pct == Decimal("0.005")
    assert "controlled_paper_risk_cap=0.005" in capped.reasons


def test_of_measurement_bucket_admits_only_clean_weak_mixed_sqz() -> None:
    signal = _signal(relative_strength="aligned", score="0.35")
    signal = replace(
        signal,
        metadata={
            **signal.metadata,
            "order_flow": {
                **signal.metadata["order_flow"],
                "alignment": "mixed",
            },
        },
    )

    admitted = _squeeze_order_flow_measurement_override(
        signal,
        _order_flow_entry_rejection_reason(signal, _config(measurement_enabled=True)),
        _config(measurement_enabled=True),
    )

    assert admitted is not None
    assert admitted.metadata["strategy"] == "SQUEEZE_BREAKOUT_OF_MEASURE"
    assert admitted.metadata["strategy_source"] == "SQUEEZE_BREAKOUT"
    assert admitted.metadata["measurement_paper"]["risk_cap_pct"] == "0.005"
    assert _controlled_paper_risk_cap(admitted) == Decimal("0.005")


def test_of_measurement_bucket_keeps_against_flow_and_dirty_retest_blocked() -> None:
    against = _signal(relative_strength="aligned", score="0.35")
    against = replace(
        against,
        metadata={
            **against.metadata,
            "order_flow": {
                **against.metadata["order_flow"],
                "alignment": "against",
            },
        },
    )
    assert _squeeze_order_flow_measurement_override(
        against,
        _order_flow_entry_rejection_reason(against, _config(measurement_enabled=True)),
        _config(measurement_enabled=True),
    ) is None

    no_retest = _signal(relative_strength="aligned", score="0.35", retest=False)
    no_retest = replace(
        no_retest,
        metadata={
            **no_retest.metadata,
            "order_flow": {
                **no_retest.metadata["order_flow"],
                "alignment": "mixed",
            },
        },
    )
    assert _squeeze_order_flow_measurement_override(
        no_retest,
        _order_flow_entry_rejection_reason(no_retest, _config(measurement_enabled=True)),
        _config(measurement_enabled=True),
    ) is None


def test_of_measurement_bucket_keeps_relative_strength_and_risk_flags_blocked() -> None:
    signal = _signal(relative_strength="neutral", score="0.35", flags=["adverse_liquidity_nearby"])
    signal = replace(
        signal,
        metadata={
            **signal.metadata,
            "order_flow": {
                **signal.metadata["order_flow"],
                "alignment": "mixed",
            },
        },
    )

    assert _squeeze_order_flow_measurement_override(
        signal,
        _order_flow_entry_rejection_reason(signal, _config(measurement_enabled=True)),
        _config(measurement_enabled=True),
    ) is None


def test_controlled_shadow_sqz_dyn_neutral_rs_is_separate_clean_retest_bucket() -> None:
    base = _signal()
    dynamic = replace(base, metadata={**base.metadata, "strategy": "SQUEEZE_BREAKOUT_DYNAMIC"})

    admitted = _controlled_shadow_sqz_dynamic_neutral_override(
        dynamic,
        _config(neutral_shadow_enabled=True),
    )

    assert admitted.metadata["controlled_shadow"]["bucket"] == "sqz_dyn_neutral_rs_retest_v1"
    assert _shadow_execution_strategy(admitted) == "SQUEEZE_BREAKOUT_DYNAMIC_NEUTRAL_RS"
    assert _shadow_candidate_context_rejection_reason(admitted, enforce_order_flow=False) is None
    execution_strategy = _shadow_execution_strategy(admitted)
    stored_signal_metadata = {**admitted.metadata, "strategy": execution_strategy}
    assert _trade_strategy({"metadata": {"signal_metadata": stored_signal_metadata}}) == execution_strategy


def test_controlled_shadow_sqz_dyn_keeps_dirty_or_against_candidates_blocked() -> None:
    base = _signal(relative_strength="against")
    dynamic = replace(base, metadata={**base.metadata, "strategy": "SQUEEZE_BREAKOUT_DYNAMIC"})

    admitted = _controlled_shadow_sqz_dynamic_neutral_override(
        dynamic,
        _config(neutral_shadow_enabled=True),
    )

    assert "controlled_shadow" not in admitted.metadata
    assert _shadow_candidate_context_rejection_reason(admitted, enforce_order_flow=False) is not None


def test_controlled_shadow_sqz_dyn_caps_risk_at_quarter_percent() -> None:
    sizing = DynamicSizingDecision(
        strategy="SQUEEZE_BREAKOUT_DYNAMIC",
        risk_pct=Decimal("0.008"),
        leverage=3,
        base_risk_pct=Decimal("0.005"),
        raw_risk_pct=Decimal("0.008"),
        multiplier=Decimal("1.6"),
        cap_risk_pct=Decimal("0.02"),
        reasons=("test",),
    )

    capped = _cap_controlled_shadow_sizing(sizing, Decimal("0.0025"))

    assert capped.risk_pct == Decimal("0.0025")
    assert capped.cap_risk_pct == Decimal("0.0025")
    assert "controlled_shadow_risk_cap=0.0025" in capped.reasons
