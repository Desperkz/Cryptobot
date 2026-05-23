from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_bot.bot import _strategy_reentry_policy_reason, _trade_cluster_metadata
from trading_bot.models import Direction, Signal, TradingStyle


def signal(strategy: str = "MEAN_REVERSION") -> Signal:
    return Signal(
        symbol="HYPEUSDT",
        direction=Direction.SHORT,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("44.66"),
        stop_loss=Decimal("44.97"),
        take_profit=Decimal("44.31"),
        confidence=Decimal("0.8"),
        reason="test",
        metadata={"strategy": strategy},
    )


def trade_row(
    *,
    status: str,
    strategy: str = "MEAN_REVERSION",
    created_at: datetime | None = None,
    closed_at: datetime | None = None,
    trade_id: int = 1,
    realized_pnl: str = "0",
    r_multiple: str = "0",
    close_reason: str | None = None,
) -> dict[str, object]:
    created_at = created_at or datetime.now(timezone.utc)
    metadata = {"signal_metadata": {"strategy": strategy}}
    return {
        "id": trade_id,
        "symbol": "HYPEUSDT",
        "direction": "SHORT",
        "status": status,
        "created_at": created_at.isoformat(),
        "closed_at": closed_at.isoformat() if closed_at else None,
        "realized_pnl": realized_pnl,
        "r_multiple": r_multiple,
        "close_reason": close_reason,
        "metadata": metadata,
    }


def test_reentry_policy_blocks_active_same_symbol_strategy() -> None:
    reason = _strategy_reentry_policy_reason(
        signal(),
        [trade_row(status="ACCEPTED", trade_id=7)],
        cooldown_minutes=45,
        winning_cooldown_minutes=30,
        scale_in_enabled=False,
        max_scale_ins_per_symbol_strategy=2,
    )

    assert reason is not None
    assert "Active HYPEUSDT MEAN_REVERSION trade #7 already exists" in reason


def test_reentry_policy_blocks_recent_closed_same_symbol_strategy() -> None:
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    reason = _strategy_reentry_policy_reason(
        signal(),
        [trade_row(status="CLOSED", closed_at=closed_at)],
        cooldown_minutes=45,
        winning_cooldown_minutes=30,
        scale_in_enabled=False,
        max_scale_ins_per_symbol_strategy=2,
    )

    assert reason is not None
    assert "re-entry cooldown" in reason


def test_reentry_policy_blocks_recent_closed_shadow_strategy_from_json_metadata() -> None:
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    row = trade_row(status="CLOSED", strategy="VWAP_REVERSION_WATCH", closed_at=closed_at, trade_id=250)
    row["metadata"] = '{"strategy":"VWAP_REVERSION_WATCH","signal_metadata":{"strategy":"VWAP_REVERSION_WATCH"}}'

    reason = _strategy_reentry_policy_reason(
        signal("VWAP_REVERSION_WATCH"),
        [row],
        cooldown_minutes=45,
        winning_cooldown_minutes=30,
        scale_in_enabled=False,
        max_scale_ins_per_symbol_strategy=0,
    )

    assert reason is not None
    assert "HYPEUSDT VWAP_REVERSION_WATCH re-entry cooldown" in reason


def test_reentry_policy_allows_after_cooldown_or_different_strategy() -> None:
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=90)
    reason = _strategy_reentry_policy_reason(
        signal(),
        [
            trade_row(status="CLOSED", closed_at=closed_at),
            trade_row(status="ACCEPTED", strategy="SQUEEZE_BREAKOUT", trade_id=9),
        ],
        cooldown_minutes=45,
        winning_cooldown_minutes=30,
        scale_in_enabled=False,
        max_scale_ins_per_symbol_strategy=2,
    )

    assert reason is None


def test_reentry_policy_uses_shorter_cooldown_after_winning_trade() -> None:
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=60)
    reason = _strategy_reentry_policy_reason(
        signal(),
        [trade_row(status="CLOSED", closed_at=closed_at, realized_pnl="12", r_multiple="0.5")],
        cooldown_minutes=180,
        winning_cooldown_minutes=45,
        scale_in_enabled=False,
        max_scale_ins_per_symbol_strategy=2,
    )

    assert reason is None


def test_reentry_policy_keeps_strict_cooldown_after_losing_trade() -> None:
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=60)
    reason = _strategy_reentry_policy_reason(
        signal(),
        [trade_row(status="CLOSED", closed_at=closed_at, realized_pnl="-12", r_multiple="-1")],
        cooldown_minutes=180,
        winning_cooldown_minutes=45,
        scale_in_enabled=False,
        max_scale_ins_per_symbol_strategy=2,
    )

    assert reason is not None
    assert "re-entry cooldown" in reason


def test_trade_cluster_metadata_reuses_recent_same_symbol_strategy_direction_cluster() -> None:
    first_signal = signal()
    first_cluster = _trade_cluster_metadata(first_signal, [], window_minutes=60)
    second_cluster = _trade_cluster_metadata(
        first_signal,
        [
            {
                **trade_row(status="CLOSED"),
                "metadata": {"signal_metadata": {"strategy": "MEAN_REVERSION"}, **first_cluster},
            }
        ],
        window_minutes=60,
    )

    assert second_cluster["trade_cluster_id"] == first_cluster["trade_cluster_id"]
    assert second_cluster["trade_cluster_sequence"] == 2
    assert second_cluster["scale_in"] is True
