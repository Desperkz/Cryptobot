from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from trading_bot.bot import (
    _annotate_shadow_order_flow_gate,
    _controlled_shadow_revalidation_override,
    _shadow_candidate_context_rejection_reason,
    _shadow_gate_counterfactual_variants,
    _shadow_parallel_lab_variants,
    _shadow_execution_strategy,
    _is_shadow_revalidation_signal,
    _shadow_paper_diagnostic_only_reason,
    _shadow_strategy_loss_control_reason,
    _sqz_dynamic_upd_series_rejection_reason,
)
from trading_bot.models import Direction, Signal, TradingStyle


def signal(
    strategy: str,
    order_flow: dict,
    *,
    symbol: str = "ADAUSDT",
    direction: Direction = Direction.SHORT,
    relative_strength: dict | None = None,
    metadata: dict | None = None,
) -> Signal:
    metadata = {"strategy": strategy, "order_flow": order_flow, **(metadata or {})}
    if relative_strength is not None:
        metadata["relative_strength"] = relative_strength
    return Signal(
        symbol=symbol,
        direction=direction,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("1.0"),
        stop_loss=Decimal("1.1"),
        take_profit=Decimal("0.9"),
        confidence=Decimal("0.55"),
        reason="test",
        metadata=metadata,
    )


def order_flow(*, alignment: str = "aligned", score: str = "0.70", flags: list[str] | None = None) -> dict:
    return {
        "alignment": alignment,
        "score": score,
        "risk_flags": flags or [],
    }


def target_flow(
    *,
    alignment: str = "aligned",
    score: str = "0.86",
    lower_bps: str = "25",
    upper_bps: str = "200",
    flags: list[str] | None = None,
) -> dict:
    return {
        **order_flow(alignment=alignment, score=score, flags=flags),
        "distance_to_lower_liquidity_bps": lower_bps,
        "distance_to_upper_liquidity_bps": upper_bps,
        "liquidity_side": "downside",
    }


def counterfactual_config(*gates: str) -> SimpleNamespace:
    return SimpleNamespace(
        shadow_gate_counterfactual_enabled=True,
        shadow_gate_counterfactual_cohort="2026-08-07-clean-of",
        shadow_gate_counterfactual_risk_cap_pct=Decimal("0.0025"),
        shadow_gate_counterfactual_gates=list(gates),
    )


def parallel_lab_config(*arms: str) -> SimpleNamespace:
    return SimpleNamespace(
        shadow_parallel_lab_enabled=True,
        shadow_parallel_lab_cohort="2026-08-14-parallel-lab",
        shadow_parallel_lab_risk_cap_pct=Decimal("0.0025"),
        shadow_parallel_lab_arms=list(arms),
    )


def test_parallel_lab_records_explicit_strict_control() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        {**order_flow(score="0.80"), "open_interest_change_pct": "0.35"},
        relative_strength={"alignment": "aligned", "score": "0.70"},
        metadata={"squeeze_retest_confirmed": True, "signal_bar_close_time": 12345},
    )

    variants, strict_reason, evaluated = _shadow_parallel_lab_variants(
        candidate,
        parallel_lab_config("STRICT", "MISSING_OI"),
    )

    assert strict_reason is None
    assert evaluated == ["STRICT"]
    assert [item.metadata["strategy"] for item in variants] == [
        "SQZ_UPD_LAB_STRICT_SHADOW"
    ]
    payload = variants[0].metadata["measurement_shadow"]
    assert payload["relaxed_gates"] == []
    assert payload["source_cluster_id"].endswith(":12345")


def test_parallel_lab_pair_does_not_mix_with_single_gate_arms() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        order_flow(score="0.80", flags=["taker_flow_against"]),
        relative_strength={"alignment": "aligned", "score": "0.70"},
        metadata={"squeeze_retest_confirmed": True, "signal_bar_close_time": 23456},
    )

    variants, strict_reason, evaluated = _shadow_parallel_lab_variants(
        candidate,
        parallel_lab_config(
            "STRICT",
            "OF_HOSTILE",
            "MISSING_OI",
            "OF_HOSTILE+MISSING_OI",
        ),
    )

    assert strict_reason is not None
    assert evaluated == ["STRICT", "OF_HOSTILE", "MISSING_OI", "OF_HOSTILE+MISSING_OI"]
    assert [item.metadata["strategy"] for item in variants] == [
        "SQZ_UPD_LAB_OF_HOSTILE_MISSING_OI_SHADOW"
    ]
    assert variants[0].metadata["measurement_shadow"]["relaxed_gates"] == [
        "MISSING_OI",
        "OF_HOSTILE",
    ]


def test_parallel_lab_can_measure_neutral_rs_and_no_retest_pair() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        {**order_flow(score="0.80"), "open_interest_change_pct": "0.35"},
        relative_strength={"alignment": "neutral", "score": "0.62"},
        metadata={
            "squeeze_retest_confirmed": False,
            "squeeze_state": "build",
            "squeeze_entry_timing": "early_breakout",
            "breakout_atr": "1.20",
        },
    )

    variants, _, evaluated = _shadow_parallel_lab_variants(
        candidate,
        parallel_lab_config(
            "STRICT",
            "RS_NEUTRAL",
            "NO_RETEST",
            "RS_NEUTRAL+NO_RETEST",
        ),
    )

    assert evaluated == ["STRICT", "RS_NEUTRAL", "NO_RETEST", "RS_NEUTRAL+NO_RETEST"]
    assert [item.metadata["strategy"] for item in variants] == [
        "SQZ_UPD_LAB_RS_NEUTRAL_NO_RETEST_SHADOW"
    ]


def test_counterfactual_sqz_upd_relaxes_only_neutral_relative_strength() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        {**order_flow(score="0.80"), "open_interest_change_pct": "0.35"},
        relative_strength={"alignment": "neutral", "score": "0.62"},
        metadata={"squeeze_retest_confirmed": True, "signal_bar_close_time": 12345},
    )

    variants, strict_reason, evaluated = _shadow_gate_counterfactual_variants(
        candidate,
        counterfactual_config("RS_NEUTRAL"),
    )

    assert strict_reason is not None
    assert evaluated == ["RS_NEUTRAL"]
    assert len(variants) == 1
    variant = variants[0]
    assert variant.metadata["strategy"] == "SQZ_DYN_UPD_CLEAN_RS_NEUTRAL_SHADOW"
    assert variant.metadata["measurement_shadow"]["relaxed_gate"] == "RS_NEUTRAL"
    assert variant.metadata["measurement_shadow"]["source_cluster_id"].endswith(":12345")
    assert _shadow_candidate_context_rejection_reason(candidate) is not None


def test_counterfactual_does_not_mix_neutral_rs_with_missing_oi() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        order_flow(score="0.80"),
        relative_strength={"alignment": "neutral", "score": "0.62"},
        metadata={"squeeze_retest_confirmed": True},
    )

    variants, strict_reason, evaluated = _shadow_gate_counterfactual_variants(
        candidate,
        counterfactual_config("RS_NEUTRAL", "MISSING_OI"),
    )

    assert strict_reason is not None
    assert evaluated == ["MISSING_OI", "RS_NEUTRAL"]
    assert variants == []


def test_counterfactual_sqz_upd_measures_missing_open_interest_only() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        order_flow(score="0.80"),
        relative_strength={"alignment": "aligned", "score": "0.70"},
        metadata={"squeeze_retest_confirmed": True},
    )

    variants, _, _ = _shadow_gate_counterfactual_variants(
        candidate,
        counterfactual_config("MISSING_OI"),
    )

    assert [item.metadata["strategy"] for item in variants] == [
        "SQZ_DYN_UPD_CLEAN_MISSING_OI_SHADOW"
    ]


def test_counterfactual_missing_oi_keeps_hostile_order_flow_strict() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        order_flow(
            alignment="against",
            score="0.18",
            flags=["taker_flow_against", "aggressive_delta_against"],
        ),
        relative_strength={"alignment": "aligned", "score": "0.70"},
        metadata={"squeeze_retest_confirmed": True},
    )

    variants, strict_reason, evaluated = _shadow_gate_counterfactual_variants(
        candidate,
        counterfactual_config("MISSING_OI"),
    )

    assert evaluated == ["MISSING_OI"]
    assert strict_reason is not None
    assert "order-flow" in strict_reason
    assert variants == []


def test_counterfactual_sqz_upd_measures_missing_retest_only() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
        {**order_flow(score="0.80"), "open_interest_change_pct": "0.35"},
        relative_strength={"alignment": "aligned", "score": "0.70"},
        metadata={
            "squeeze_retest_confirmed": False,
            "squeeze_state": "build",
            "squeeze_entry_timing": "early_breakout",
            "breakout_atr": "1.20",
        },
    )

    variants, _, _ = _shadow_gate_counterfactual_variants(
        candidate,
        counterfactual_config("NO_RETEST"),
    )

    assert [item.metadata["strategy"] for item in variants] == [
        "SQZ_DYN_UPD_CLEAN_NO_RETEST_SHADOW"
    ]


def test_counterfactual_tpb_measures_near_target_liquidity_only() -> None:
    candidate = signal(
        "TREND_PULLBACK",
        target_flow(score="0.70", lower_bps="4.2"),
        relative_strength={"alignment": "aligned", "score": "0.66"},
        metadata={"pullback_depth_atr": "0.90", "volume_ratio": "1.50"},
    )

    variants, _, _ = _shadow_gate_counterfactual_variants(
        candidate,
        counterfactual_config("NEAR_LIQUIDITY"),
    )

    assert [item.metadata["strategy"] for item in variants] == [
        "TPB_CLEAN_NEAR_LIQUIDITY_SHADOW"
    ]


def test_counterfactual_tpb_measures_against_relative_strength_only() -> None:
    candidate = signal(
        "TREND_PULLBACK",
        target_flow(score="0.70", lower_bps="25"),
        relative_strength={"alignment": "against", "score": "0.66"},
        metadata={"pullback_depth_atr": "0.90", "volume_ratio": "1.50"},
    )

    variants, _, _ = _shadow_gate_counterfactual_variants(
        candidate,
        counterfactual_config("RS_AGAINST"),
    )

    assert [item.metadata["strategy"] for item in variants] == [
        "TPB_CLEAN_RS_AGAINST_SHADOW"
    ]


def test_lsr_shadow_context_blocks_adverse_liquidity() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "LIQUIDITY_SWEEP_REVERSAL",
            order_flow(score="0.78", flags=["adverse_liquidity_nearby"]),
        )
    )

    assert reason is not None
    assert "adverse liquidity" in reason


def test_lsr_shadow_context_blocks_weak_order_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("LIQUIDITY_SWEEP_REVERSAL", order_flow(score="0.42"))
    )

    assert reason is not None
    assert "not strong enough" in reason


def test_lsr_shadow_context_allows_research_follow_through_sample() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("LIQUIDITY_SWEEP_REVERSAL", order_flow(score="0.68"))
    ) is None


def test_vwr_watch_shadow_context_blocks_dangerous_liquidity_flags() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("VWAP_REVERSION_WATCH", order_flow(score="0.80", flags=["liquidation_cascade"]))
    )

    assert reason is not None
    assert "dangerous liquidity context" in reason


def test_vwap_shadow_context_requires_clean_reversion_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("VWAP_REVERSION", order_flow(score="0.38"))
    )

    assert reason is not None
    assert "not clean enough" in reason


def test_vwap_watch_shadow_allows_research_sample_with_nearby_liquidity() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("VWAP_REVERSION_WATCH", order_flow(score="0.46", flags=["adverse_liquidity_nearby"]))
    ) is None


def test_grid_shadow_context_blocks_against_order_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("RANGE_GRID", order_flow(alignment="against", score="0.50"))
    )

    assert reason is not None
    assert "against range fade" in reason


def test_grid_shadow_context_blocks_dangerous_range_edge_flags() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("RANGE_GRID", order_flow(alignment="aligned", score="0.68", flags=["structure_break_against"]))
    )

    assert reason is not None
    assert "dangerous range-edge flow" in reason


def test_grid_shadow_context_allows_research_sample_with_minor_flow_flags() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("RANGE_GRID", order_flow(alignment="aligned", score="0.34", flags=["adverse_liquidity_nearby"]))
    ) is None


def test_sqz_dynamic_shadow_blocks_order_flow_against_breakout() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("SQUEEZE_BREAKOUT_DYNAMIC", order_flow(alignment="against", score="0.18", flags=["taker_flow_against"]))
    )

    assert reason is not None
    assert "against breakout" in reason


def test_sqz_dynamic_shadow_records_opposed_flow_when_gate_is_observe_only() -> None:
    candidate = signal(
        "SQUEEZE_BREAKOUT_DYNAMIC",
        order_flow(alignment="against", score="0.18", flags=["taker_flow_against"]),
        relative_strength={"alignment": "aligned"},
        metadata={"squeeze_retest_confirmed": True},
    )

    strict_reason = _shadow_candidate_context_rejection_reason(candidate)
    observe_only_reason = _shadow_candidate_context_rejection_reason(
        candidate,
        enforce_order_flow=False,
    )

    assert strict_reason is not None
    assert "against breakout" in strict_reason
    assert observe_only_reason is None


def test_shadow_order_flow_observation_is_persistable_in_signal_metadata() -> None:
    marked = _annotate_shadow_order_flow_gate(
        signal("SQUEEZE_BREAKOUT_DYNAMIC", order_flow(alignment="against")),
        strict_rejection_reason="SQZ-DYN shadow blocked: order-flow is against breakout.",
        execution_rejection_reason=None,
        enforced=False,
        overridden=True,
    )

    assert marked.metadata["shadow_order_flow_gate"] == {
        "enforced": False,
        "would_block": True,
        "would_block_reason": "SQZ-DYN shadow blocked: order-flow is against breakout.",
        "overridden_for_research": True,
        "execution_eligible": True,
        "structural_rejection_reason": None,
    }


def test_sqz_dynamic_shadow_requires_retest_for_mixed_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC",
            order_flow(alignment="mixed", score="0.56"),
            relative_strength={"alignment": "aligned"},
            metadata={
                "squeeze_retest_confirmed": False,
                "squeeze_state": "build",
                "squeeze_entry_timing": "early_breakout",
                "breakout_atr": "0.80",
            },
        )
    )

    assert reason is not None
    assert "no retest" in reason


def test_sqz_dynamic_shadow_allows_clean_aligned_flow() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "aligned"},
            metadata={"squeeze_retest_confirmed": True},
        )
    ) is None


def test_sqz_dynamic_upd_blocks_no_retest_when_target_liquidity_is_too_close() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.76"),
        "distance_to_lower_liquidity_bps": "7.8",
        "reasons": ["target_liquidity_nearby", "structure_break_aligned"],
    }

    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            flow,
            relative_strength={"alignment": "aligned"},
            metadata={"squeeze_retest_confirmed": False, "open_interest_change_pct": "0.35"},
        )
    )

    assert reason is not None
    assert "target liquidity is too close without retest" in reason


def test_sqz_dynamic_original_now_requires_relative_strength_and_retest() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.76"),
        "distance_to_lower_liquidity_bps": "7.8",
        "reasons": ["target_liquidity_nearby", "structure_break_aligned"],
    }

    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC",
            flow,
            metadata={"squeeze_retest_confirmed": False},
        )
    )

    assert reason is not None
    assert "relative-strength confirmation" in reason


def test_sqz_dynamic_upd_allows_near_liquidity_after_confirmed_retest() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.76"),
        "distance_to_lower_liquidity_bps": "7.8",
        "open_interest_change_pct": "0.35",
        "reasons": ["target_liquidity_nearby", "structure_break_aligned"],
    }

    assert _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            flow,
            relative_strength={"alignment": "aligned"},
            metadata={"squeeze_retest_confirmed": True},
        )
    ) is None


def test_sqz_dynamic_upd_blocks_unknown_relative_strength() -> None:
    flow = {**order_flow(alignment="aligned", score="0.80"), "open_interest_change_pct": "0.42"}

    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            flow,
            relative_strength={"alignment": "unknown"},
            metadata={
                "squeeze_retest_confirmed": True,
                "squeeze_state": "release",
                "squeeze_entry_timing": "release_followthrough",
                "breakout_atr": "1.80",
            },
        )
    )

    assert reason is not None
    assert "relative-strength confirmation" in reason


def test_sqz_dynamic_upd_blocks_missing_open_interest_confirmation() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            order_flow(alignment="aligned", score="0.80"),
            relative_strength={"alignment": "aligned"},
            metadata={
                "squeeze_retest_confirmed": True,
                "squeeze_state": "release",
                "squeeze_entry_timing": "release_followthrough",
                "breakout_atr": "1.80",
            },
        )
    )

    assert reason is not None
    assert "open-interest confirmation" in reason


def test_sqz_dynamic_upd_blocks_absorption_against_even_with_aligned_flow() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.72", flags=["absorption_against"]),
        "open_interest_change_pct": "0.35",
    }

    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            flow,
            relative_strength={"alignment": "aligned"},
            metadata={
                "squeeze_retest_confirmed": False,
                "squeeze_state": "release",
                "squeeze_entry_timing": "release_followthrough",
                "breakout_atr": "1.80",
            },
        )
    )

    assert reason is not None
    assert "absorption against breakout" in reason


def test_sqz_dynamic_upd_blocks_weak_no_retest_release() -> None:
    flow = {**order_flow(alignment="aligned", score="0.70"), "open_interest_change_pct": "0.35"}

    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            flow,
            relative_strength={"alignment": "aligned"},
            metadata={
                "squeeze_retest_confirmed": False,
                "squeeze_state": "build",
                "squeeze_entry_timing": "early_breakout",
                "breakout_atr": "1.20",
            },
        )
    )

    assert reason is not None
    assert "no retest" in reason


def test_sqz_dynamic_upd_allows_strong_release_without_retest() -> None:
    flow = {**order_flow(alignment="aligned", score="0.76"), "open_interest_change_pct": "0.35"}

    assert _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            flow,
            relative_strength={"alignment": "aligned"},
            metadata={
                "squeeze_retest_confirmed": False,
                "squeeze_state": "release",
                "squeeze_entry_timing": "release_followthrough",
                "breakout_atr": "1.80",
            },
        )
    ) is None


def test_sqz_dynamic_upd_blocks_third_same_direction_cluster() -> None:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    trades = [
        {"strategy": "SQUEEZE_BREAKOUT_DYNAMIC_UPD", "direction": "SHORT", "created_at": created_at},
        {"strategy": "SQUEEZE_BREAKOUT_DYNAMIC_UPD", "direction": "SHORT", "created_at": created_at},
    ]

    reason = _sqz_dynamic_upd_series_rejection_reason(
        signal=signal("SQUEEZE_BREAKOUT_DYNAMIC_UPD", order_flow()),
        trades=trades,
        window_minutes=90,
        max_same_direction_trades=2,
    )

    assert reason is not None
    assert "same-direction cluster cap" in reason


def test_tpb_shadow_does_not_quarantine_symbol_when_bucket_is_clean() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            symbol="LTCUSDT",
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.50"},
        )
    ) is None


def test_tpb_shadow_blocks_mixed_order_flow_outside_profitable_bucket() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="mixed", score="0.70"),
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.50"},
        )
    )

    assert reason is not None
    assert "profitable bucket requires aligned order-flow" in reason


def test_tpb_shadow_blocks_weak_mixed_order_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.44"),
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.50"},
        )
    )

    assert reason is not None
    assert "order-flow score" in reason


def test_tpb_shadow_blocks_short_without_relative_weakness_confirmation() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "unknown"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.50"},
        )
    )

    assert reason is not None
    assert "relative-weakness confirmation" in reason


def test_tpb_shadow_allows_clean_long_continuation() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.66"),
            direction=Direction.LONG,
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.50"},
        )
    ) is None


def test_tpb_shadow_blocks_long_without_relative_strength_confirmation() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.66"),
            direction=Direction.LONG,
            relative_strength={"alignment": "unknown"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.50"},
        )
    )

    assert reason is not None
    assert "relative-strength confirmation" in reason


def test_tpb_shadow_blocks_unprofitable_pullback_depth_bucket() -> None:
    shallow = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "0.30", "volume_ratio": "1.50"},
        )
    )
    extended = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "2.10", "volume_ratio": "1.50"},
        )
    )

    assert shallow is not None
    assert "too shallow" in shallow
    assert extended is not None
    assert "too extended" in extended


def test_tpb_shadow_blocks_low_volume_bucket() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.05"},
        )
    )

    assert reason is not None
    assert "volume ratio" in reason


def test_tpb_shadow_blocks_low_relative_strength_score() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "aligned", "score": "0.58"},
            metadata={"pullback_depth_atr": "0.90", "volume_ratio": "1.50"},
        )
    )

    assert reason is not None
    assert "relative-strength score" in reason


def test_tpb_shadow_blocks_close_target_liquidity() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            target_flow(score="0.70", lower_bps="4.2"),
            relative_strength={"alignment": "aligned", "score": "0.62"},
            metadata={"pullback_depth_atr": "0.90", "volume_ratio": "1.50"},
        )
    )

    assert reason is not None
    assert "target liquidity" in reason


def test_momentum_shadow_blocks_low_relative_strength_score() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "MOMENTUM_CONTINUATION",
            target_flow(score="0.86", lower_bps="25"),
            relative_strength={"alignment": "aligned", "score": "0.58"},
            metadata={"breakout_extension_atr": "0.30"},
        )
    )

    assert reason is not None
    assert "relative-strength score" in reason


def test_momentum_shadow_blocks_close_target_liquidity() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "MOMENTUM_CONTINUATION",
            target_flow(score="0.86", lower_bps="4.7"),
            relative_strength={"alignment": "aligned", "score": "0.66"},
            metadata={"breakout_extension_atr": "0.30"},
        )
    )

    assert reason is not None
    assert "target liquidity" in reason


def test_momentum_shadow_blocks_weak_breakout_extension() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "MOMENTUM_CONTINUATION",
            target_flow(score="0.86", lower_bps="25"),
            relative_strength={"alignment": "aligned", "score": "0.66"},
            metadata={"breakout_extension_atr": "0.08"},
        )
    )

    assert reason is not None
    assert "breakout extension" in reason


def test_momentum_shadow_allows_clean_continuation_sample() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal(
            "MOMENTUM_CONTINUATION",
            target_flow(score="0.86", lower_bps="25"),
            relative_strength={"alignment": "aligned", "score": "0.66"},
            metadata={"breakout_extension_atr": "0.30"},
        )
    ) is None


def test_trend_following_shadow_allows_unknown_relative_strength_for_edge_research_sample() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_FOLLOWING",
            order_flow(alignment="aligned", score="0.86"),
            relative_strength={"alignment": "unknown"},
        )
    ) is None


def test_trend_following_shadow_blocks_relative_strength_against() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_FOLLOWING",
            order_flow(alignment="aligned", score="0.86"),
            relative_strength={"alignment": "against"},
        )
    )

    assert reason is not None
    assert "relative-weakness confirmation" in reason


def test_trend_following_shadow_blocks_too_close_target_liquidity() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.86"),
        "liquidity_side": "downside",
        "distance_to_lower_liquidity_bps": "5.4",
    }
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_FOLLOWING",
            flow,
            relative_strength={"alignment": "aligned"},
            metadata={"open_interest_change_pct": "0.35"},
        )
    )

    assert reason is not None
    assert "liquidity is already too close" in reason


def test_trend_following_shadow_blocks_low_atr_continuation() -> None:
    trend_signal = signal(
        "TREND_FOLLOWING",
        order_flow(alignment="aligned", score="0.86"),
        relative_strength={"alignment": "aligned"},
        metadata={"open_interest_change_pct": "0.35"},
    )
    trend_signal.metadata["atr_pct"] = "0.31"

    reason = _shadow_candidate_context_rejection_reason(trend_signal)

    assert reason is not None
    assert "ATR" in reason


def test_trend_following_shadow_allows_clean_continuation_sample() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.86"),
        "liquidity_side": "downside",
        "distance_to_lower_liquidity_bps": "25",
    }
    trend_signal = signal(
        "TREND_FOLLOWING",
        flow,
        relative_strength={"alignment": "aligned"},
        metadata={"open_interest_change_pct": "0.35"},
    )
    trend_signal.metadata["atr_pct"] = "0.48"

    assert _shadow_candidate_context_rejection_reason(trend_signal) is None


def test_trend_following_shadow_allows_missing_open_interest_for_edge_research_sample() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_FOLLOWING",
            order_flow(alignment="aligned", score="0.86"),
            relative_strength={"alignment": "aligned"},
        )
    ) is None


def test_negative_expectancy_shadow_strategies_are_diagnostic_only() -> None:
    reason = _shadow_paper_diagnostic_only_reason(
        signal("RANGE_GRID", order_flow(alignment="aligned", score="0.86"))
    )

    assert reason is not None
    assert "diagnostics only" in reason


def test_configured_shadow_revalidation_isolated_from_historical_bucket() -> None:
    config = SimpleNamespace(
        shadow_revalidation_enabled=True,
        shadow_revalidation_cohort="2026-08-01",
        shadow_revalidation_risk_cap_pct=Decimal("0.0025"),
        shadow_revalidation_strategies=["RANGE_GRID"],
    )

    revalidation_signal = _controlled_shadow_revalidation_override(
        signal("RANGE_GRID", order_flow(alignment="aligned", score="0.86")),
        config,
    )

    assert revalidation_signal.metadata["strategy"] == "RANGE_GRID"
    assert _is_shadow_revalidation_signal(revalidation_signal) is True
    assert _shadow_execution_strategy(revalidation_signal) == "GRID_REVALIDATION"
    assert _shadow_paper_diagnostic_only_reason(revalidation_signal, config) is None
    assert revalidation_signal.metadata["controlled_shadow"]["risk_cap_pct"] == "0.0025"


def test_edge_candidate_shadow_strategies_can_open_shadow_paper() -> None:
    reason = _shadow_paper_diagnostic_only_reason(
        signal("TREND_FOLLOWING", order_flow(alignment="aligned", score="0.86"))
    )

    assert reason is None


def test_shadow_context_gate_ignores_non_candidate_strategy() -> None:
    assert _shadow_candidate_context_rejection_reason(signal("SQUEEZE_BREAKOUT", order_flow(alignment="against"))) is None


def test_shadow_strategy_loss_control_blocks_recent_drawdown() -> None:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    trades = [
        {
            "strategy": "MOMENTUM_CONTINUATION",
            "status": "CLOSED",
            "created_at": created_at,
            "closed_at": created_at,
            "r_multiple": "-1.04",
            "realized_pnl": "-16.0",
        },
        {
            "strategy": "MOMENTUM_CONTINUATION",
            "status": "CLOSED",
            "created_at": created_at,
            "closed_at": created_at,
            "r_multiple": "-1.05",
            "realized_pnl": "-16.2",
        },
    ]

    reason = _shadow_strategy_loss_control_reason(
        signal=signal("MOMENTUM_CONTINUATION", order_flow()),
        trades=trades,
        window_hours=168,
        min_closed_trades=2,
        max_total_r=Decimal("-1.50"),
        max_loss_count=2,
        loss_count_max_total_r=Decimal("-1.00"),
    )

    assert reason is not None
    assert "loss-control" in reason


def test_shadow_strategy_loss_control_ignores_other_strategies() -> None:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    trades = [
        {
            "strategy": "VWAP_REVERSION_WATCH",
            "status": "CLOSED",
            "created_at": created_at,
            "closed_at": created_at,
            "r_multiple": "-2.00",
            "realized_pnl": "-30.0",
        }
    ]

    reason = _shadow_strategy_loss_control_reason(
        signal=signal("MOMENTUM_CONTINUATION", order_flow()),
        trades=trades,
        window_hours=168,
        min_closed_trades=2,
        max_total_r=Decimal("-1.50"),
        max_loss_count=2,
        loss_count_max_total_r=Decimal("-1.00"),
    )

    assert reason is None
