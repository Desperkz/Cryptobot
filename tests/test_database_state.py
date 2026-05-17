from __future__ import annotations

import pytest

from trading_bot.database.sqlite import Database
from trading_bot.models import Direction, Signal, TradingStyle


@pytest.mark.asyncio
async def test_risk_state_persists_pnl_date(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.sqlite3'}")
    await db.connect()
    try:
        await db.save_risk_state(2, "2026-05-12T10:00:00+00:00", "-42.5", "2026-05-12")
        saved = await db.load_risk_state()
    finally:
        await db.close()

    assert saved is not None
    assert saved["losing_streak"] == 2
    assert saved["realized_pnl_today"] == "-42.5"
    assert saved["pnl_date_utc"] == "2026-05-12"


@pytest.mark.asyncio
async def test_filter_rejections_are_persisted(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.sqlite3'}")
    await db.connect()
    try:
        await db.insert_filter_rejection("BTCUSDT", "LONG", "SQZ", "0.80", "OI", "test")
        rows = await db.recent_rejections()
    finally:
        await db.close()

    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["filter_type"] == "OI"


@pytest.mark.asyncio
async def test_shadow_trades_are_persisted_separately_from_real_paper_trades(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.sqlite3'}")
    signal = Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        confidence=0.75,
        reason="shadow candidate",
        metadata={"strategy": "TREND_PULLBACK", "strategy_mode": "shadow"},
    )
    await db.connect()
    try:
        await db.insert_shadow_trade(
            signal=signal,
            strategy="TREND_PULLBACK",
            quantity="4",
            risk_amount="20",
            metadata={"shadow_paper": True},
        )
        shadow_rows = await db.recent_shadow_trades()
        real_rows = await db.recent_trades()
        has_open = await db.has_open_shadow_trade("BTCUSDT", "TREND_PULLBACK")
    finally:
        await db.close()

    assert has_open is True
    assert shadow_rows[0]["symbol"] == "BTCUSDT"
    assert shadow_rows[0]["strategy"] == "TREND_PULLBACK"
    assert shadow_rows[0]["mode"] == "SHADOW_PAPER"
    assert real_rows == []
