from __future__ import annotations

import json

from bot_control_v2 import (
    apply_strategy_promotion_policy,
    build_chaos_readiness_report,
    build_monthly_target_report,
    build_production_readiness_report,
    build_strategy_allocator,
    build_strategy_scorecard,
    build_weekly_research_report,
    evaluate_shadow_gate,
)


def trade(
    *,
    trade_id: int,
    strategy: str,
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    status: str = "CLOSED",
    pnl: str = "0",
    r_value: str = "0",
    created_at: str = "2026-01-01 00:00:00",
    closed_at: str | None = None,
    quantity: str = "1",
    entry_price: str = "100",
    risk_amount: str = "10",
    nested_metadata: bool = True,
    realistic_execution: bool = True,
    exit_profile_signature: str | None = None,
    first_target_net_r: str | None = None,
) -> dict[str, str | int | None]:
    metadata = {"signal_metadata": {"strategy": strategy}} if nested_metadata else {"strategy": strategy}
    signal_metadata = metadata["signal_metadata"] if nested_metadata else metadata
    if exit_profile_signature is not None:
        signal_metadata["exit_profile_signature"] = exit_profile_signature
    if first_target_net_r is not None:
        signal_metadata["first_target_net_reward_risk"] = first_target_net_r
    if realistic_execution:
        metadata["paper_execution_summary"] = {
            "gross_pnl": pnl,
            "fees": "0",
            "slippage_cost": "0",
            "funding_cost": "0",
            "net_pnl": pnl,
        }
    return {
        "id": trade_id,
        "created_at": created_at,
        "closed_at": closed_at,
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "entry_price": entry_price,
        "stop_loss": "95",
        "take_profit": "110",
        "mode": "PAPER_TRADING",
        "status": status,
        "risk_amount": risk_amount,
        "r_multiple": r_value,
        "realized_pnl": pnl,
        "metadata": json.dumps(metadata),
    }


def rejection(strategy: str, filter_type: str = "UTC") -> dict[str, str]:
    return {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "strategy": strategy,
        "confidence": "0.8",
        "filter_type": filter_type,
        "reason": "test",
        "created_at": "2026-01-01 00:01:00",
    }


def signal_row(strategy: str, strategy_mode: str = "shadow", metadata: dict | None = None) -> dict[str, str]:
    payload = {"strategy": strategy, "strategy_mode": strategy_mode}
    if metadata:
        payload.update(metadata)
    return {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "confidence": "0.8",
        "reason": "shadow candidate",
        "created_at": "2026-01-01 00:05:00",
        "metadata": json.dumps(payload),
    }


def diagnostic_row(strategy: str, reason: str, symbol: str = "BTCUSDT") -> dict[str, str]:
    return {
        "symbol": symbol,
        "direction": "NONE",
        "strategy": strategy,
        "confidence": "0",
        "decision": "STRATEGY_DIAGNOSTIC",
        "reason": reason,
        "created_at": "2026-01-01 00:06:00",
        "metadata": json.dumps({"strategy": strategy, "diagnostic": True, "block_reason": reason}),
    }


def order_flow_row(
    strategy: str,
    *,
    symbol: str = "BTCUSDT",
    alignment: str = "aligned",
    score: float = 0.72,
    risk_flags: list[str] | None = None,
) -> dict[str, str]:
    payload = {
        "alignment": alignment,
        "score": score,
        "flow_bias": "LONG",
        "liquidity_side": "upside",
        "risk_flags": risk_flags or [],
        "reasons": ["taker_flow_aligned"],
    }
    return {
        "symbol": symbol,
        "direction": "LONG",
        "strategy": strategy,
        "confidence": "0.7",
        "decision": "ORDER_FLOW_ANNOTATION",
        "reason": f"alignment={alignment}; score={score}",
        "features": json.dumps(payload),
        "metadata": json.dumps({"strategy": strategy, "order_flow": payload}),
        "created_at": "2026-01-01 00:07:00",
    }


def relative_strength_row(
    strategy: str,
    *,
    symbol: str = "BTCUSDT",
    alignment: str = "aligned",
    score: float = 0.76,
) -> dict[str, str]:
    payload = {
        "alignment": alignment,
        "score": score,
        "symbol_change_4h": "0.018",
        "btc_change_4h": "0.004",
        "relative_change_4h": "0.014",
        "symbol_change_24h": "0.052",
        "reasons": ["long_relative_strength"],
    }
    return {
        "symbol": symbol,
        "direction": "LONG",
        "strategy": strategy,
        "confidence": "0.7",
        "decision": "RELATIVE_STRENGTH_ANNOTATION",
        "reason": f"alignment={alignment}; score={score}",
        "features": json.dumps(payload),
        "metadata": json.dumps({"strategy": strategy, "relative_strength": payload}),
        "created_at": "2026-01-01 00:08:00",
    }


def shadow_trade(
    *,
    strategy: str,
    status: str,
    pnl: str = "0",
    r_value: str = "0",
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    created_at: str = "2026-01-01 00:10:00",
    closed_at: str | None = None,
    metadata: dict | None = None,
) -> dict[str, str | None]:
    payload = {"strategy": strategy, "shadow_paper": True, **(metadata or {})}
    return {
        "id": "1",
        "created_at": created_at,
        "closed_at": closed_at,
        "symbol": symbol,
        "direction": direction,
        "strategy": strategy,
        "quantity": "4",
        "entry_price": "100",
        "stop_loss": "95",
        "take_profit": "110",
        "mode": "SHADOW_PAPER",
        "status": status,
        "risk_amount": "20",
        "r_multiple": r_value,
        "realized_pnl": pnl,
        "close_reason": "take_profit" if status == "CLOSED" else None,
        "metadata": json.dumps(payload),
    }


def by_strategy(scorecard: dict, name: str) -> dict:
    return next(item for item in scorecard["strategies"] if item["strategy"] == name)


def test_strategy_scorecard_splits_closed_open_and_rejections() -> None:
    trades = [
        trade(trade_id=1, strategy="SQUEEZE_BREAKOUT", pnl="20", r_value="1.2", closed_at="2026-01-01 01:00:00"),
        trade(trade_id=2, strategy="SQUEEZE_BREAKOUT", pnl="-5", r_value="-1", created_at="2026-01-02 00:00:00"),
        trade(
            trade_id=3,
            strategy="SQUEEZE_BREAKOUT",
            symbol="XRPUSDT",
            direction="SHORT",
            status="OPEN",
            quantity="10",
            entry_price="2.0",
            risk_amount="5",
        ),
        trade(
            trade_id=4,
            strategy="MEAN_REVERSION",
            pnl="-3",
            r_value="-0.5",
            nested_metadata=False,
        ),
    ]
    rejections = [
        rejection("SQUEEZE_BREAKOUT", "UTC"),
        rejection("SQUEEZE_BREAKOUT", "UTC"),
        rejection("SQUEEZE_BREAKOUT", "OI"),
        rejection("TREND_PULLBACK", "RISK"),
    ]

    scorecard = build_strategy_scorecard(trades, rejections, initial_equity=1000, prices={"XRPUSDT": "1.9"})
    sqz = by_strategy(scorecard, "SQUEEZE_BREAKOUT")

    assert scorecard["summary"]["strategies"] == 3
    assert sqz["closed_trades"] == 2
    assert sqz["open_trades"] == 1
    assert sqz["wins"] == 1
    assert sqz["losses"] == 1
    assert sqz["winrate"] == 50.0
    assert sqz["gross_profit"] == 20
    assert sqz["gross_loss"] == 5
    assert sqz["profit_factor"] == 4
    assert sqz["realized_pnl"] == 15
    assert sqz["unrealized_pnl"] == 1
    assert sqz["total_pnl"] == 16
    assert sqz["avg_r"] == 0.1
    assert sqz["open_risk"] == 5
    assert sqz["open_positions"][0]["pnl_r"] == 0.2
    assert sqz["rejections_total"] == 3
    assert sqz["rejections_by_type"] == {"OI": 1, "UTC": 2}
    assert sqz["by_symbol"]["BTCUSDT"]["realized_pnl"] == 15
    assert sqz["by_direction"]["LONG"]["closed_trades"] == 2
    assert sqz["max_drawdown"] == -0.49
    assert sqz["gate"]["status"] == "WATCH"
    assert sqz["gate"]["promotion_allowed"] is False
    assert "min_closed_trades" in sqz["gate"]["failed_checks"]
    assert "min_sample_age_days" in sqz["gate"]["failed_checks"]

    mean_reversion = by_strategy(scorecard, "MEAN_REVERSION")
    assert mean_reversion["gate"]["status"] == "BLOCKED"
    assert "min_winrate" in mean_reversion["gate"]["failed_checks"]
    assert "min_profit_factor" in mean_reversion["gate"]["failed_checks"]
    assert "min_avg_r" in mean_reversion["gate"]["failed_checks"]


def test_scorecard_keeps_rejection_only_candidate_strategies() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [rejection("TREND_PULLBACK", "RISK")],
        [signal_row("TREND_PULLBACK", "shadow")],
        initial_equity=1000,
        strategy_modes={"TREND_PULLBACK": "shadow"},
    )
    row = by_strategy(scorecard, "TREND_PULLBACK")

    assert row["closed_trades"] == 0
    assert row["open_trades"] == 0
    assert row["strategy_mode"] == "shadow"
    assert row["signals_total"] == 1
    assert row["shadow_signals"] == 1
    assert row["realized_pnl"] == 0
    assert row["rejections_total"] == 1
    assert row["rejections_by_type"] == {"RISK": 1}
    assert row["gate"]["status"] == "WATCH"
    assert "no_closed_trades" in row["gate"]["failed_checks"]


def test_strategy_gate_ignores_pre_p5_ideal_fills_for_promotion() -> None:
    scorecard = build_strategy_scorecard(
        [
            trade(
                trade_id=1,
                strategy="SQUEEZE_BREAKOUT",
                pnl="20",
                r_value="1.0",
                created_at="2026-01-01 00:00:00",
                closed_at="2026-01-01 01:00:00",
                realistic_execution=False,
            ),
            trade(
                trade_id=2,
                strategy="SQUEEZE_BREAKOUT",
                pnl="10",
                r_value="0.5",
                created_at="2026-01-03 00:00:00",
                closed_at="2026-01-03 01:00:00",
                realistic_execution=False,
            ),
        ],
        [],
        initial_equity=1000,
        gate_thresholds={
            "min_closed_trades": 1,
            "min_sample_age_days": 0,
            "min_closed_trades_per_day": 0,
            "min_winrate": 1,
            "min_profit_factor": 0,
            "min_avg_r": -1,
            "max_drawdown": -100,
        },
    )

    row = by_strategy(scorecard, "SQUEEZE_BREAKOUT")

    assert row["closed_trades"] == 2
    assert row["pre_p5_closed_trades"] == 2
    assert row["post_p5_evidence"]["closed_trades"] == 0
    assert row["post_p5_evidence"]["closed_trade_clusters"] == 0
    assert row["gate"]["evidence_scope"] == "post_p5_realistic_execution"
    assert row["gate"]["status"] == "WATCH"
    assert row["gate"]["promotion_allowed"] is False
    assert "min_closed_trades" in row["gate"]["failed_checks"]
    assert "no_closed_trades" in row["gate"]["failed_checks"]
    assert scorecard["summary"]["post_p5_closed_trades"] == 0
    assert scorecard["summary"]["pre_p5_closed_trades"] == 2


def test_scorecard_breaks_down_post_p5_by_exit_profile_signature() -> None:
    scorecard = build_strategy_scorecard(
        [
            trade(
                trade_id=1,
                strategy="SQUEEZE_BREAKOUT",
                pnl="10",
                r_value="0.5",
                closed_at="2026-01-01 01:00:00",
                exit_profile_signature="old_tp",
                first_target_net_r="0.2",
            ),
            trade(
                trade_id=2,
                strategy="SQUEEZE_BREAKOUT",
                pnl="-20",
                r_value="-1.0",
                closed_at="2026-01-02 01:00:00",
                exit_profile_signature="new_runner",
                first_target_net_r="0.6",
            ),
            trade(
                trade_id=3,
                strategy="SQUEEZE_BREAKOUT",
                pnl="30",
                r_value="1.5",
                closed_at="2026-01-03 01:00:00",
                exit_profile_signature="new_runner",
                first_target_net_r="0.7",
            ),
        ],
        [],
        initial_equity=1000,
    )

    row = by_strategy(scorecard, "SQUEEZE_BREAKOUT")
    breakdown = row["post_p5_evidence"]["exit_profile_breakdown"]

    assert breakdown[0]["exit_profile_signature"] == "new_runner"
    assert breakdown[0]["closed_trades"] == 2
    assert breakdown[0]["realized_pnl"] == 10
    assert breakdown[0]["avg_first_target_net_r"] == 0.65
    assert breakdown[1]["exit_profile_signature"] == "old_tp"


def test_scorecard_includes_configured_shadow_strategies_before_first_signal() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [],
        [],
        [],
        initial_equity=1000,
        strategy_modes={
            "LIQUIDITY_SWEEP_REVERSAL": "shadow",
            "VWAP_REVERSION": "shadow",
        },
    )

    names = {row["strategy"] for row in scorecard["strategies"]}
    assert names == {"LIQUIDITY_SWEEP_REVERSAL", "VWAP_REVERSION"}
    lsr = by_strategy(scorecard, "LIQUIDITY_SWEEP_REVERSAL")
    assert lsr["strategy_mode"] == "shadow"
    assert lsr["closed_trades"] == 0
    assert lsr["signals_total"] == 0
    assert lsr["shadow_gate"]["status"] == "TESTING"


def test_scorecard_summarizes_shadow_candidate_evidence() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [],
        [
            signal_row(
                "MEAN_REVERSION",
                "shadow",
                {
                    "mr_confluence": "6",
                    "divergence": "yes",
                    "volume_ok": "True",
                    "edge_confirms": "True",
                    "reversal_candle": "False",
                    "mr_confirmation_flags": ["atr_deviation", "divergence", "volume", "edge"],
                },
            ),
            signal_row(
                "MEAN_REVERSION",
                "shadow",
                {
                    "mr_confluence": "5",
                    "divergence": "yes",
                    "volume_ok": "True",
                    "edge_confirms": "False",
                    "reversal_candle": "True",
                    "mr_confirmation_flags": ["atr_deviation", "divergence", "volume", "reversal_candle"],
                },
            ),
        ],
        initial_equity=1000,
        strategy_modes={"MEAN_REVERSION": "shadow"},
    )

    row = by_strategy(scorecard, "MEAN_REVERSION")

    assert row["signals_total"] == 2
    assert row["shadow_signals"] == 2
    assert row["candidate_evidence"]["avg_signal_confidence"] == 0.8
    assert row["candidate_evidence"]["avg_confluence"] == 5.5
    assert row["candidate_evidence"]["counts"]["divergence"] == 2
    assert row["candidate_evidence"]["counts"]["volume_confirmed"] == 2
    assert row["candidate_evidence"]["counts"]["edge_confirmed"] == 1
    assert row["candidate_evidence"]["counts"]["reversal_candle"] == 1


def test_scorecard_summarizes_strategy_diagnostics() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [],
        [],
        [],
        [
            diagnostic_row("VWAP_REVERSION", "no_reversal_confirmation"),
            diagnostic_row("VWAP_REVERSION", "no_reversal_confirmation", symbol="ETHUSDT"),
            diagnostic_row("VWAP_REVERSION", "flow_against_reversion"),
        ],
        initial_equity=1000,
        strategy_modes={"VWAP_REVERSION": "shadow"},
    )

    row = by_strategy(scorecard, "VWAP_REVERSION")
    diagnostics = row["candidate_evidence"]["diagnostics"]

    assert diagnostics["total"] == 3
    assert diagnostics["by_reason"]["no_reversal_confirmation"] == 2
    assert diagnostics["by_reason"]["flow_against_reversion"] == 1
    assert diagnostics["top_symbols"]["BTCUSDT"] == 2


def test_scorecard_summarizes_order_flow_annotations() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [],
        [],
        [],
        [
            order_flow_row("SQUEEZE_BREAKOUT", alignment="aligned", score=0.8),
            order_flow_row(
                "SQUEEZE_BREAKOUT",
                symbol="ETHUSDT",
                alignment="against",
                score=0.2,
                risk_flags=["liquidation_cascade", "taker_flow_against"],
            ),
        ],
        initial_equity=1000,
        strategy_modes={"SQUEEZE_BREAKOUT": "paper"},
    )

    row = by_strategy(scorecard, "SQUEEZE_BREAKOUT")
    order_flow = row["candidate_evidence"]["order_flow"]

    assert order_flow["total"] == 2
    assert order_flow["avg_score"] == 0.5
    assert order_flow["by_alignment"] == {"aligned": 1, "against": 1}
    assert order_flow["risk_flags"]["liquidation_cascade"] == 1
    assert order_flow["risk_flags"]["taker_flow_against"] == 1
    assert order_flow["top_symbols"] == {"BTCUSDT": 1, "ETHUSDT": 1}


def test_scorecard_summarizes_relative_strength_annotations() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [],
        [],
        [],
        [
            relative_strength_row("TREND_PULLBACK", alignment="aligned", score=0.8),
            relative_strength_row("TREND_PULLBACK", symbol="ETHUSDT", alignment="against", score=0.3),
        ],
        initial_equity=1000,
        strategy_modes={"TREND_PULLBACK": "shadow"},
    )

    row = by_strategy(scorecard, "TREND_PULLBACK")
    relative_strength = row["candidate_evidence"]["relative_strength"]

    assert relative_strength["total"] == 2
    assert relative_strength["avg_score"] == 0.55
    assert relative_strength["by_alignment"] == {"aligned": 1, "against": 1}
    assert relative_strength["top_symbols"] == {"BTCUSDT": 1, "ETHUSDT": 1}


def test_scorecard_summarizes_shadow_paper_without_polluting_real_pnl() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [],
        [signal_row("TREND_PULLBACK", "shadow")],
        [
            shadow_trade(strategy="TREND_PULLBACK", status="CLOSED", pnl="12", r_value="0.6"),
            {
                **shadow_trade(strategy="TREND_PULLBACK", status="OPEN", symbol="ETHUSDT"),
                "id": "2",
                "entry_price": "100",
                "quantity": "2",
                "risk_amount": "10",
            },
        ],
        initial_equity=1000,
        prices={"ETHUSDT": "103"},
        strategy_modes={"TREND_PULLBACK": "shadow"},
    )

    row = by_strategy(scorecard, "TREND_PULLBACK")

    assert row["closed_trades"] == 0
    assert row["realized_pnl"] == 0
    assert row["total_pnl"] == 0
    assert row["shadow_paper"]["closed_trades"] == 1
    assert row["shadow_paper"]["open_trades"] == 1
    assert row["shadow_paper"]["realized_pnl"] == 12
    assert row["shadow_paper"]["unrealized_pnl"] == 6
    assert row["shadow_paper"]["total_pnl"] == 18
    assert row["shadow_paper"]["winrate"] == 100.0
    assert scorecard["summary"]["total_pnl"] == 0
    assert scorecard["summary"]["shadow_total_pnl"] == 18
    assert row["shadow_gate"]["status"] == "TESTING"


def test_scorecard_breaks_down_shadow_evidence_by_strategy_logic_version() -> None:
    scorecard = build_strategy_scorecard(
        [],
        [],
        [signal_row("SQUEEZE_BREAKOUT_DYNAMIC", "shadow")],
        [
            shadow_trade(
                strategy="SQUEEZE_BREAKOUT_DYNAMIC",
                status="CLOSED",
                pnl="-8",
                r_value="-1",
            ),
            shadow_trade(
                strategy="SQUEEZE_BREAKOUT_DYNAMIC",
                status="CLOSED",
                pnl="14",
                r_value="0.7",
                metadata={"strategy_logic_version": "sqz_dyn_of_retest_v2"},
            ),
        ],
        initial_equity=1000,
        strategy_modes={"SQUEEZE_BREAKOUT_DYNAMIC": "shadow"},
    )

    row = by_strategy(scorecard, "SQUEEZE_BREAKOUT_DYNAMIC")
    breakdown = {
        item["strategy_logic_version"]: item
        for item in row["shadow_paper"]["strategy_logic_version_breakdown"]
    }

    assert breakdown["legacy"]["closed_trades"] == 1
    assert breakdown["legacy"]["realized_pnl"] == -8
    assert breakdown["sqz_dyn_of_retest_v2"]["closed_trades"] == 1
    assert breakdown["sqz_dyn_of_retest_v2"]["realized_pnl"] == 14


def test_shadow_gate_promotes_when_shadow_paper_thresholds_pass() -> None:
    gate = evaluate_shadow_gate(
        {
            "closed_trades": 35,
            "sample_age_days": 4,
            "winrate": 51,
            "profit_factor": 1.4,
            "avg_r": 0.12,
            "max_drawdown": -4,
        }
    )

    assert gate["status"] == "PROMOTE"
    assert gate["recommendation"] == "PROMOTE_TO_PAPER"
    assert gate["promotion_candidate"] is True


def test_scorecard_counts_fast_repeat_entries_as_one_trade_cluster() -> None:
    trades = [
        trade(
            trade_id=7,
            strategy="MEAN_REVERSION",
            symbol="HYPEUSDT",
            direction="SHORT",
            pnl="20.73",
            r_value="1.1",
            created_at="2026-05-14 20:06:00",
        ),
        trade(
            trade_id=8,
            strategy="MEAN_REVERSION",
            symbol="HYPEUSDT",
            direction="SHORT",
            pnl="21.11",
            r_value="1.1",
            created_at="2026-05-14 20:08:00",
        ),
        trade(
            trade_id=9,
            strategy="MEAN_REVERSION",
            symbol="HYPEUSDT",
            direction="SHORT",
            pnl="21.50",
            r_value="1.1",
            created_at="2026-05-14 20:09:00",
        ),
        trade(
            trade_id=10,
            strategy="MEAN_REVERSION",
            symbol="HYPEUSDT",
            direction="SHORT",
            pnl="21.89",
            r_value="1.1",
            created_at="2026-05-14 20:11:00",
        ),
    ]
    scorecard = build_strategy_scorecard(
        trades,
        [],
        initial_equity=1000,
        gate_thresholds={
            "min_closed_trades": 2,
            "min_sample_age_days": 0,
            "min_closed_trades_per_day": 0,
            "min_winrate": 40,
            "min_profit_factor": 1.25,
            "min_avg_r": 0,
            "max_drawdown": -10,
        },
    )

    row = by_strategy(scorecard, "MEAN_REVERSION")
    assert row["closed_trades"] == 4
    assert row["closed_trade_clusters"] == 1
    assert row["trade_clusters"]["largest_size"] == 4
    assert row["trade_clusters"]["multi_trade_clusters"] == 1
    assert row["cluster_wins"] == 1
    assert row["cluster_winrate"] == 100.0
    assert scorecard["summary"]["closed_trades"] == 4
    assert scorecard["summary"]["closed_trade_clusters"] == 1
    assert "min_closed_trades" in row["gate"]["failed_checks"]
    assert next(c for c in row["gate"]["checks"] if c["id"] == "min_closed_trades")["value"] == 1


def test_strategy_gate_allows_promotion_when_thresholds_pass() -> None:
    trades = [
        trade(
            trade_id=1,
            strategy="SQUEEZE_BREAKOUT",
            pnl="20",
            r_value="1.0",
            created_at="2026-01-01 00:00:00",
            closed_at="2026-01-01 01:00:00",
        ),
        trade(
            trade_id=2,
            strategy="SQUEEZE_BREAKOUT",
            pnl="10",
            r_value="0.5",
            created_at="2026-01-03 00:00:00",
            closed_at="2026-01-03 01:00:00",
        ),
    ]
    scorecard = build_strategy_scorecard(
        trades,
        [],
        initial_equity=1000,
        gate_thresholds={
            "min_closed_trades": 2,
            "min_sample_age_days": 1,
            "min_closed_trades_per_day": 0.1,
            "min_winrate": 40,
            "min_profit_factor": 1.25,
            "min_avg_r": 0,
            "max_drawdown": -10,
        },
    )

    row = by_strategy(scorecard, "SQUEEZE_BREAKOUT")
    assert row["sample_age_days"] == 2.0
    assert row["gate"]["status"] == "PROMOTABLE"
    assert row["gate"]["promotion_allowed"] is True
    assert row["gate"]["failed_checks"] == []


def test_strategy_allocator_is_advisory_and_ranks_positive_evidence() -> None:
    scorecard = {
        "generated_at": "2026-05-18T00:00:00+00:00",
        "strategies": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "strategy_mode": "paper",
                "closed_trade_clusters": 8,
                "closed_trades": 8,
                "cluster_winrate": 75,
                "cluster_profit_factor": 2.4,
                "cluster_avg_r": 0.32,
                "max_drawdown": -2.0,
                "total_pnl": 80,
                "open_trades": 0,
                "open_risk": 0,
                "gate": {"status": "WATCH"},
            },
            {
                "strategy": "VWAP_REVERSION_WATCH",
                "strategy_mode": "shadow",
                "shadow_gate": {"status": "PROMOTE", "promotion_candidate": True},
                "shadow_paper": {
                    "closed_trades": 35,
                    "open_trades": 0,
                    "winrate": 57,
                    "profit_factor": 1.6,
                    "avg_r": 0.18,
                    "max_drawdown": -4,
                    "total_pnl": 42,
                    "open_risk": 0,
                },
            },
            {
                "strategy": "RANGE_GRID",
                "strategy_mode": "shadow",
                "shadow_gate": {"status": "WATCH", "promotion_candidate": False},
                "shadow_paper": {
                    "closed_trades": 20,
                    "open_trades": 0,
                    "winrate": 55,
                    "profit_factor": 0.8,
                    "avg_r": -0.05,
                    "max_drawdown": -8,
                    "total_pnl": -12,
                    "open_risk": 0,
                },
            },
        ],
    }

    allocator = build_strategy_allocator(scorecard)
    by_name = {row["strategy"]: row for row in allocator["allocations"]}

    assert allocator["mode"] == "ADVISORY_ONLY"
    assert allocator["auto_switching_enabled"] is False
    assert by_name["SQUEEZE_BREAKOUT"]["action"] == "CHAMPION_WATCH"
    assert by_name["SQUEEZE_BREAKOUT"]["suggested_risk_weight_pct"] > 0
    assert by_name["VWAP_REVERSION_WATCH"]["action"] == "PROMOTION_REVIEW"
    assert by_name["VWAP_REVERSION_WATCH"]["max_risk_weight_pct"] == 10
    assert by_name["RANGE_GRID"]["action"] == "RESEARCH_ONLY"
    assert by_name["RANGE_GRID"]["suggested_risk_weight_pct"] == 0


def test_promotion_policy_flags_mean_reversion_for_review_at_30_clusters_not_live() -> None:
    trades = [
        trade(
            trade_id=i,
            strategy="MEAN_REVERSION",
            symbol=f"MR{i}USDT",
            pnl="10",
            r_value="0.5",
            created_at=f"2026-01-{(i % 28) + 1:02d} 00:00:00",
            closed_at=f"2026-01-{(i % 28) + 1:02d} 01:00:00",
        )
        for i in range(1, 41)
    ]
    scorecard = build_strategy_scorecard(
        trades,
        [],
        initial_equity=1000,
        gate_thresholds={
            "min_closed_trades": 20,
            "min_sample_age_days": 1,
            "min_closed_trades_per_day": 0,
            "min_winrate": 40,
            "min_profit_factor": 1.25,
            "min_avg_r": 0,
            "max_drawdown": -10,
        },
        strategy_modes={"MEAN_REVERSION": "paper"},
    )

    row = by_strategy(scorecard, "MEAN_REVERSION")

    assert row["gate"]["status"] == "PROMOTABLE"
    assert row["promotion_policy"]["tier"] == "PAPER_ONLY"
    assert row["promotion_policy"]["action"] == "PAPER_ALLOCATION_REVIEW_REQUIRED"
    assert row["promotion_policy"]["paper_review_allowed"] is True
    assert row["promotion_policy"]["live_review_allowed"] is False
    assert row["promotion_policy"]["post_p5_clusters"] < 200


def test_promotion_policy_keeps_mean_reversion_collecting_before_30_clusters() -> None:
    trades = [
        trade(
            trade_id=i,
            strategy="MEAN_REVERSION",
            symbol=f"MR{i}USDT",
            pnl="10",
            r_value="0.5",
            created_at=f"2026-01-{(i % 28) + 1:02d} 00:00:00",
            closed_at=f"2026-01-{(i % 28) + 1:02d} 01:00:00",
        )
        for i in range(1, 21)
    ]
    scorecard = build_strategy_scorecard(
        trades,
        [],
        initial_equity=1000,
        strategy_modes={"MEAN_REVERSION": "paper"},
    )

    row = by_strategy(scorecard, "MEAN_REVERSION")

    assert row["promotion_policy"]["action"] == "COLLECT_PAPER_EVIDENCE"
    assert row["promotion_policy"]["paper_review_allowed"] is False
    assert row["promotion_policy"]["live_review_allowed"] is False


def test_promotion_policy_requires_human_review_for_trend_pullback_shadow_promotion() -> None:
    row = {
        "strategy": "TREND_PULLBACK",
        "strategy_mode": "shadow",
        "gate": {"status": "WATCH"},
        "shadow_gate": {"status": "PROMOTE", "promotion_candidate": True},
        "shadow_paper": {"closed_trades": 35},
        "post_p5_evidence": {"closed_trade_clusters": 0},
    }

    policy = apply_strategy_promotion_policy(row)

    assert policy["tier"] == "SHADOW_REVIEW"
    assert policy["action"] == "HUMAN_REVIEW_FOR_PAPER"
    assert policy["paper_review_allowed"] is True
    assert policy["live_review_allowed"] is False
    assert policy["human_review_required"] is True
    assert policy["paper_trial"]["duration_days"] == 30
    assert policy["paper_trial"]["max_allocation_pct"] == 5
    assert any("limited paper trial" in reason for reason in policy["reasons"])


def test_promotion_policy_blocks_research_strategy_even_when_shadow_gate_promotes() -> None:
    row = {
        "strategy": "VWAP_REVERSION_WATCH",
        "strategy_mode": "shadow",
        "gate": {"status": "WATCH"},
        "shadow_gate": {"status": "PROMOTE", "promotion_candidate": True},
        "shadow_paper": {"closed_trades": 40},
        "post_p5_evidence": {"closed_trade_clusters": 0},
    }

    policy = apply_strategy_promotion_policy(row)

    assert policy["tier"] == "RESEARCH"
    assert policy["action"] == "RESEARCH_RETEST_BEFORE_PAPER"
    assert policy["paper_review_allowed"] is False
    assert policy["live_review_allowed"] is False


def test_weekly_research_report_ranks_and_flags_actions() -> None:
    scorecard = {
        "generated_at": "2026-05-21T00:00:00+00:00",
        "strategies": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "strategy_mode": "paper",
                "post_p5_evidence": {
                    "closed_trade_clusters": 12,
                    "realized_pnl": 44,
                    "cluster_winrate": 70,
                    "cluster_profit_factor": 1.8,
                    "cluster_avg_r": 0.18,
                    "max_drawdown": -3,
                },
                "candidate_evidence": {"order_flow": {"avg_score": 0.62}},
                "promotion_policy": {
                    "tier": "CHAMPION",
                    "action": "KEEP_CHAMPION_UNDER_REVIEW",
                    "paper_review_allowed": False,
                    "live_review_allowed": False,
                    "reasons": ["needs post-P5 evidence"],
                },
            },
            {
                "strategy": "RANGE_GRID",
                "strategy_mode": "shadow",
                "shadow_paper": {
                    "closed_trades": 18,
                    "open_trades": 0,
                    "total_pnl": -14,
                    "winrate": 55,
                    "profit_factor": 0.8,
                    "avg_r": -0.04,
                    "max_drawdown": -6,
                },
                "candidate_evidence": {"order_flow": {"avg_score": 0.28}},
                "promotion_policy": {
                    "tier": "RESEARCH",
                    "action": "KEEP_RESEARCH",
                    "paper_review_allowed": False,
                    "live_review_allowed": False,
                    "reasons": ["research only"],
                },
            },
        ],
    }
    allocator = {
        "mode": "ADVISORY_ONLY",
        "allocations": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "evidence_source": "paper",
                "action": "CHAMPION_WATCH",
                "policy_action": "KEEP_CHAMPION_UNDER_REVIEW",
                "suggested_risk_weight_pct": 12.5,
                "max_risk_weight_pct": 45,
            },
            {
                "strategy": "RANGE_GRID",
                "evidence_source": "shadow_paper",
                "action": "RESEARCH_ONLY",
                "policy_action": "KEEP_RESEARCH",
                "suggested_risk_weight_pct": 0,
                "max_risk_weight_pct": 0,
            },
        ],
    }
    promotions = {
        "candidates": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "recommendation": "KEEP_CHAMPION_UNDER_REVIEW",
                "policy_tier": "CHAMPION",
            }
        ]
    }
    order_flow = {"summary": {"total": 25, "avg_score": 0.55}}
    ml_report = {
        "validated": True,
        "baseline": {"trades": 20},
        "ml_filtered": {"trades": 14},
        "comparison": {"total_r_improvement": 1.2},
    }

    report = build_weekly_research_report(scorecard, allocator, promotions, order_flow, ml_report)

    assert report["mode"] == "ADVISORY_ONLY"
    assert report["summary"]["strategies"] == 2
    assert report["summary"]["promotion_candidates"] == 1
    assert report["summary"]["anomalies"] == 2
    assert report["ranking"][0]["strategy"] == "SQUEEZE_BREAKOUT"
    assert report["ranking"][0]["allocation"]["suggested_risk_weight_pct"] == 12.5
    assert report["anomalies"][0]["strategy"] == "RANGE_GRID"
    assert report["ml"]["validated"] is True
    assert report["recommendations"]


def test_production_readiness_blocks_until_evidence_is_complete() -> None:
    scorecard = {
        "summary": {"post_p5_closed_trades": 42},
        "strategies": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "post_p5_evidence": {
                    "closed_trade_clusters": 11,
                    "cluster_profit_factor": 1.94,
                    "cluster_winrate": 81.8,
                    "cluster_avg_r": 0.033,
                    "max_drawdown": -3.1,
                },
            }
        ],
    }

    report = build_production_readiness_report(scorecard)

    assert report["status"] == "BLOCKED"
    blocker_ids = {item["id"] for item in report["blockers"]}
    assert "post_p5_closed_trades" in blocker_ids
    assert "squeeze_breakout_closed_clusters" in blocker_ids
    assert "squeeze_breakout_avg_r" in blocker_ids
    assert "testnet_restart_recovery" in blocker_ids
    assert "chaos_scenarios" in blocker_ids
    assert "production_unlock" in blocker_ids


def complete_chaos_evidence() -> dict:
    return {
        "source": "unit-test",
        "scenarios": {
            "control_api_timeout": {"status": "PASS"},
            "dashboard_status_debounce": {"status": "PASS"},
            "service_restart_recovery": {"status": "PASS"},
            "binance_timeout_backoff": {"status": "PASS"},
            "network_loss_recovery": {"status": "PASS"},
            "stale_user_stream_rest_fallback": {"status": "PASS"},
            "vps_reboot_recovery": {"status": "PASS"},
        },
        "duplicate_orders_after_chaos": 0,
        "unprotected_positions_after_chaos": 0,
        "critical_incidents_after_chaos": 0,
    }


def test_chaos_readiness_blocks_missing_or_partial_evidence() -> None:
    report = build_chaos_readiness_report({
        "source": "unit-test",
        "scenarios": {
            "control_api_timeout": {"status": "PASS"},
            "dashboard_status_debounce": {"status": "PASS"},
        },
        "duplicate_orders_after_chaos": 0,
        "unprotected_positions_after_chaos": 0,
        "critical_incidents_after_chaos": 0,
    })

    assert report["status"] == "BLOCKED"
    blocker_ids = {item["id"] for item in report["blockers"]}
    assert "service_restart_recovery" in blocker_ids
    assert "vps_reboot_recovery" in blocker_ids


def test_chaos_readiness_passes_complete_evidence() -> None:
    report = build_chaos_readiness_report(complete_chaos_evidence())

    assert report["status"] == "PASS"
    assert report["passed"] is True
    assert report["summary"]["blocked"] == 0


def test_production_readiness_passes_with_complete_evidence() -> None:
    scorecard = {
        "summary": {"post_p5_closed_trades": 520},
        "strategies": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "post_p5_evidence": {
                    "closed_trade_clusters": 120,
                    "cluster_profit_factor": 1.55,
                    "cluster_winrate": 48.0,
                    "cluster_avg_r": 0.31,
                    "max_drawdown": -7.4,
                },
            }
        ],
    }
    testnet_evidence = {
        "lifecycle": {
            "entry": True,
            "stop_loss": True,
            "take_profit": True,
            "cancel": True,
            "partial_fill": True,
            "restart_recovery": True,
        },
        "duplicate_orders": 0,
        "unprotected_positions": 0,
        "critical_incidents": 0,
        "soak_days": 14,
    }
    production_unlock = {
        "human_approved_by": "operator",
        "backtest_approved": True,
        "paper_trading_approved": True,
    }

    report = build_production_readiness_report(scorecard, testnet_evidence, complete_chaos_evidence(), production_unlock)

    assert report["status"] == "READY"
    assert report["ready_for_mainnet"] is True
    assert report["summary"]["blocked"] == 0


def test_monthly_target_report_blocks_negative_expectancy() -> None:
    scorecard = {
        "summary": {},
        "strategies": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "strategy_mode": "paper",
                "open_trades": 0,
                "post_p5_evidence": {
                    "closed_trade_clusters": 8,
                    "sample_age_days": 3.2,
                    "cluster_avg_r": -0.4,
                    "cluster_profit_factor": 0.25,
                    "cluster_winrate": 50,
                    "realized_pnl": -40,
                },
            }
        ],
    }

    report = build_monthly_target_report(
        scorecard,
        initial_equity=1000,
        target_monthly_return_pct=10,
        base_risk_pct=0.02,
    )

    assert report["summary"]["status"] == "BELOW_TARGET"
    row = report["strategies"][0]
    assert row["status"] == "BLOCKED"
    assert "non_positive_avg_r" in row["blockers"]
    assert row["primary_blocker"] == "sample"
    assert row["projected_monthly_return_pct"] < 0


def test_monthly_target_report_estimates_required_avg_r() -> None:
    scorecard = {
        "summary": {},
        "strategies": [
            {
                "strategy": "TREND_PULLBACK",
                "strategy_mode": "shadow",
                "shadow_paper": {
                    "closed_trades": 30,
                    "open_trades": 0,
                    "sample_age_days": 10,
                    "avg_r": 0.25,
                    "profit_factor": 1.6,
                    "winrate": 55,
                    "realized_pnl": 120,
                },
            }
        ],
    }

    report = build_monthly_target_report(
        scorecard,
        initial_equity=1000,
        target_monthly_return_pct=10,
        base_risk_pct=0.02,
    )

    row = report["strategies"][0]
    assert row["monthly_clusters_estimate"] == 90
    assert row["projected_monthly_r"] == 22.5
    assert row["projected_monthly_return_pct"] == 45.0
    assert row["required_avg_r_at_current_frequency"] == 0.056
    assert row["required_monthly_clusters_at_current_avg_r"] == 20
    assert row["monthly_cluster_gap_at_current_avg_r"] == 0
    assert row["avg_r_gap_at_current_frequency"] == 0
    assert row["primary_blocker"] == "none"
    assert row["status"] == "WATCH_SAMPLE"


def test_monthly_target_report_marks_frequency_gap() -> None:
    scorecard = {
        "summary": {},
        "strategies": [
            {
                "strategy": "SQUEEZE_BREAKOUT",
                "strategy_mode": "paper",
                "open_trades": 0,
                "post_p5_evidence": {
                    "closed_trade_clusters": 40,
                    "sample_age_days": 60,
                    "cluster_avg_r": 0.2,
                    "cluster_profit_factor": 1.4,
                    "cluster_winrate": 55,
                    "realized_pnl": 80,
                },
            }
        ],
    }

    report = build_monthly_target_report(
        scorecard,
        initial_equity=1000,
        target_monthly_return_pct=10,
        base_risk_pct=0.02,
    )

    row = report["strategies"][0]
    assert row["monthly_clusters_estimate"] == 20
    assert row["required_monthly_clusters_at_current_avg_r"] == 25
    assert row["monthly_cluster_gap_at_current_avg_r"] == 5
    assert row["primary_blocker"] == "frequency"
