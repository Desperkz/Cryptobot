from __future__ import annotations

import json

import pytest

from trading_bot.analytics.sqz_cohort_reporting import (
    SQZCohortMilestoneReporter,
    SQZ_PAPER_MEASUREMENT,
)
from trading_bot.database.sqlite import Database as SQLiteDatabase


class FakeDatabase:
    def __init__(self, *, shadow_rows: list[dict] | None = None, paper_rows: list[dict] | None = None) -> None:
        self.shadow_rows = shadow_rows or []
        self.paper_rows = paper_rows or []
        self.states: dict[str, str] = {}

    async def closed_shadow_trades_by_strategies(self, strategies, closed_after):
        assert closed_after == "2026-07-26T03:11:47+00:00"
        return [row for row in self.shadow_rows if row["strategy"] in strategies]

    async def recent_trades(self, limit):
        assert limit == 10_000
        return self.paper_rows

    async def load_operational_state(self, key):
        return self.states.get(key)

    async def save_operational_state(self, key, value):
        self.states[key] = value


class FakeTelegram:
    enabled = True

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text, **_metadata):
        self.messages.append(text)


def shadow_row(strategy: str, r_multiple: float) -> dict:
    return {
        "strategy": strategy,
        "r_multiple": str(r_multiple),
        "risk_amount": "10",
        "realized_pnl": str(r_multiple * 10),
        "closed_at": "2026-07-27 12:00:00",
    }


def paper_row(r_multiple: float) -> dict:
    return {
        "status": "CLOSED",
        "r_multiple": str(r_multiple),
        "risk_amount": "10",
        "realized_pnl": str(r_multiple * 10),
        "closed_at": "2026-07-27 12:00:00",
        "metadata": json.dumps({"signal_metadata": {"strategy": SQZ_PAPER_MEASUREMENT}}),
    }


@pytest.mark.asyncio
async def test_shadow_cohort_sends_once_at_20_trade_milestone() -> None:
    db = FakeDatabase(shadow_rows=[shadow_row("SQZ_OF_AGAINST_SHADOW", 0.25) for _ in range(20)])
    telegram = FakeTelegram()
    reporter = SQZCohortMilestoneReporter(
        db,
        telegram,
        stats_epoch="2026-07-26T03:11:47+00:00",
        check_interval_sec=60,
    )

    await reporter.maybe_report(db.paper_rows)
    reporter._next_check_at = 0
    await reporter.maybe_report(db.paper_rows)

    assert len(telegram.messages) == 1
    assert "SQZ_OF_AGAINST_SHADOW" in telegram.messages[0]
    assert "threshold: 20" in telegram.messages[0]
    assert "EARLY_SIGNAL" in telegram.messages[0]
    assert len(db.states) == 1


@pytest.mark.asyncio
async def test_paper_measurement_warns_for_negative_20_trade_bucket() -> None:
    db = FakeDatabase(paper_rows=[paper_row(-1.0) for _ in range(20)])
    telegram = FakeTelegram()
    reporter = SQZCohortMilestoneReporter(
        db,
        telegram,
        stats_epoch="2026-07-26T03:11:47+00:00",
    )

    await reporter.maybe_report(db.paper_rows)

    assert len(telegram.messages) == 1
    assert SQZ_PAPER_MEASUREMENT in telegram.messages[0]
    assert "STOP_REVIEW" in telegram.messages[0]
    assert "no automatic promotion" in telegram.messages[0]


@pytest.mark.asyncio
async def test_sqlite_cohort_query_and_operational_state_are_persistent(tmp_path) -> None:
    db = SQLiteDatabase(f"sqlite+aiosqlite:///{tmp_path / 'cohorts.sqlite3'}")
    await db.connect()
    try:
        conn = db._require_conn()
        await conn.execute(
            """
            INSERT INTO shadow_trades(
                strategy, symbol, direction, quantity, entry_price, stop_loss, take_profit,
                status, risk_amount, r_multiple, realized_pnl, closed_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?)
            """,
            (
                "SQZ_OF_AGAINST_SHADOW",
                "BTCUSDT",
                "LONG",
                "1",
                "100",
                "99",
                "102",
                "10",
                "0.5",
                "5",
                "2026-07-27 12:00:00",
                "{}",
            ),
        )
        await conn.commit()

        rows = await db.closed_shadow_trades_by_strategies(
            ["SQZ_OF_AGAINST_SHADOW"],
            "2026-07-26T03:11:47+00:00",
        )
        await db.save_operational_state("p7-17:test", "observed")

        assert len(rows) == 1
        assert rows[0]["r_multiple"] == "0.5"
        assert await db.load_operational_state("p7-17:test") == "observed"
    finally:
        await db.close()
