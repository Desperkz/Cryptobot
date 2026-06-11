from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.bot import _shadow_candidate_context_rejection_reason, _sqz_dynamic_upd_series_rejection_reason
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


def test_sqz_dynamic_shadow_requires_retest_for_mixed_flow() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal("SQUEEZE_BREAKOUT_DYNAMIC", order_flow(alignment="mixed", score="0.56"))
    )

    assert reason is not None
    assert "retest confirmation" in reason


def test_sqz_dynamic_shadow_allows_clean_aligned_flow() -> None:
    assert _shadow_candidate_context_rejection_reason(
        signal("SQUEEZE_BREAKOUT_DYNAMIC", order_flow(alignment="aligned", score="0.70"))
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
            metadata={"squeeze_retest_confirmed": False},
        )
    )

    assert reason is not None
    assert "target liquidity is too close without retest" in reason


def test_sqz_dynamic_original_keeps_near_liquidity_sample_for_comparison() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.76"),
        "distance_to_lower_liquidity_bps": "7.8",
        "reasons": ["target_liquidity_nearby", "structure_break_aligned"],
    }

    assert _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC",
            flow,
            metadata={"squeeze_retest_confirmed": False},
        )
    ) is None


def test_sqz_dynamic_upd_allows_near_liquidity_after_confirmed_retest() -> None:
    flow = {
        **order_flow(alignment="aligned", score="0.76"),
        "distance_to_lower_liquidity_bps": "7.8",
        "reasons": ["target_liquidity_nearby", "structure_break_aligned"],
    }

    assert _shadow_candidate_context_rejection_reason(
        signal(
            "SQUEEZE_BREAKOUT_DYNAMIC_UPD",
            flow,
            metadata={"squeeze_retest_confirmed": True},
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
            relative_strength={"alignment": "aligned"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.50"},
        )
    ) is None


def test_tpb_shadow_blocks_mixed_order_flow_outside_profitable_bucket() -> None:
    reason = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="mixed", score="0.70"),
            relative_strength={"alignment": "aligned"},
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
            relative_strength={"alignment": "aligned"},
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
            relative_strength={"alignment": "aligned"},
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
            relative_strength={"alignment": "aligned"},
            metadata={"pullback_depth_atr": "0.30", "volume_ratio": "1.50"},
        )
    )
    extended = _shadow_candidate_context_rejection_reason(
        signal(
            "TREND_PULLBACK",
            order_flow(alignment="aligned", score="0.70"),
            relative_strength={"alignment": "aligned"},
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
            relative_strength={"alignment": "aligned"},
            metadata={"pullback_depth_atr": "0.75", "volume_ratio": "1.05"},
        )
    )

    assert reason is not None
    assert "volume ratio" in reason


def test_trend_following_shadow_allows_unknown_relative_strength_for_research_sample() -> None:
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
        )
    )

    assert reason is not None
    assert "liquidity is already too close" in reason


def test_trend_following_shadow_blocks_low_atr_continuation() -> None:
    trend_signal = signal(
        "TREND_FOLLOWING",
        order_flow(alignment="aligned", score="0.86"),
        relative_strength={"alignment": "aligned"},
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
    )
    trend_signal.metadata["atr_pct"] = "0.48"

    assert _shadow_candidate_context_rejection_reason(trend_signal) is None


def test_shadow_context_gate_ignores_non_candidate_strategy() -> None:
    assert _shadow_candidate_context_rejection_reason(signal("SQUEEZE_BREAKOUT", order_flow(alignment="against"))) is None
