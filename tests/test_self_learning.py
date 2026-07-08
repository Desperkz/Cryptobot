from __future__ import annotations

from decimal import Decimal

from trading_bot.analytics.self_learning import SelfLearningEngine
from trading_bot.config import AnalyticsConfig


def analytics_config() -> AnalyticsConfig:
    return AnalyticsConfig(
        min_trades_for_adaptation=1,
        min_expectancy_r=Decimal("0"),
        min_winrate=Decimal("0"),
        disable_symbol_after_bad_trades=1,
        segment_min_trades=1,
        bad_segment_expectancy_r=Decimal("0"),
    )


def test_self_learning_ignores_open_trades() -> None:
    engine = SelfLearningEngine(analytics_config())
    trades = [
        {
            "symbol": "BTCUSDT",
            "status": "OPEN",
            "realized_pnl": "-50",
            "risk_amount": "10",
            "metadata": {"signal_metadata": {"hour_utc": "12", "style": "SQUEEZE_BREAKOUT"}},
        },
        {
            "symbol": "ETHUSDT",
            "status": "CLOSED",
            "realized_pnl": "10",
            "risk_amount": "10",
            "metadata": {"signal_metadata": {"hour_utc": "12", "style": "SQUEEZE_BREAKOUT"}},
        },
    ]

    payload = engine.adaptive_thresholds(trades)

    assert payload["blocked_symbols"] == []
    assert payload["blocked_symbol_hours"] == []
    assert payload["recommendations"] == []


def test_trades_since_stats_epoch_filters_old_closed_trades() -> None:
    from trading_bot.bot import _trades_since_stats_epoch

    trades = [
        {"id": 1, "created_at": "2026-06-01 09:00:00", "closed_at": "2026-06-01 10:00:00", "status": "CLOSED"},
        {"id": 2, "created_at": "2026-07-09 09:00:00", "closed_at": "2026-07-09 10:00:00", "status": "CLOSED"},
        {"id": 3, "created_at": "2026-07-09 09:00:00", "closed_at": None, "status": "OPEN"},
        # Opened before the deploy epoch and closed after it: this is a hybrid
        # old-logic trade and must not teach the new self-learning rules.
        {"id": 4, "created_at": "2026-07-07 23:00:00", "closed_at": "2026-07-09 10:00:00", "status": "CLOSED"},
        {"id": 5, "closed_at": None, "status": "CLOSED"},
    ]
    filtered = _trades_since_stats_epoch(trades, "2026-07-08")
    assert [t["id"] for t in filtered] == [2, 3]

    # без эпохи — вся история, как раньше
    assert _trades_since_stats_epoch(trades, None) == trades
    # нечитаемая эпоха — фильтр не применяется, а не роняет цикл
    assert _trades_since_stats_epoch(trades, "not-a-date") == trades
