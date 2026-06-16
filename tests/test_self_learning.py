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
