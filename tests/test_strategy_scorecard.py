from __future__ import annotations

import json

from bot_control_v2 import build_strategy_scorecard, evaluate_shadow_gate


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
) -> dict[str, str | int | None]:
    metadata = {"signal_metadata": {"strategy": strategy}} if nested_metadata else {"strategy": strategy}
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
) -> dict[str, str | None]:
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
        "metadata": json.dumps({"strategy": strategy, "shadow_paper": True}),
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
