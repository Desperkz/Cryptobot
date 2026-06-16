from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any


class PostgresDatabase:
    def __init__(self, url: str) -> None:
        self.url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._pool: Any | None = None

    async def connect(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL mode requires asyncpg. Install with: pip install asyncpg") from exc
        self._pool = await asyncpg.create_pool(self.url)
        await self.migrate()

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def migrate(self) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    style TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    stop_loss TEXT NOT NULL,
                    take_profit TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    metadata JSONB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    stop_loss TEXT NOT NULL,
                    take_profit TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_amount TEXT DEFAULT '0',
                    r_multiple TEXT DEFAULT '0',
                    realized_pnl TEXT DEFAULT '0',
                    closed_at TIMESTAMPTZ,
                    metadata JSONB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    closed_at TIMESTAMPTZ,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    stop_loss TEXT NOT NULL,
                    take_profit TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'SHADOW_PAPER',
                    status TEXT NOT NULL,
                    risk_amount TEXT DEFAULT '0',
                    r_multiple TEXT DEFAULT '0',
                    realized_pnl TEXT DEFAULT '0',
                    close_reason TEXT,
                    metadata JSONB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    losing_streak INTEGER DEFAULT 0,
                    cooldown_until TEXT,
                    realized_pnl_today TEXT DEFAULT '0',
                    pnl_date_utc TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
            await conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE risk_state ADD COLUMN IF NOT EXISTS pnl_date_utc TEXT")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS filter_rejections (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    filter_type TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ml_feature_snapshots (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    features JSONB NOT NULL,
                    metadata JSONB NOT NULL
                );
                """
            )

    async def insert_signal(self, signal: Any) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO signals(symbol, direction, style, entry_price, stop_loss, take_profit, confidence, reason, metadata)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                signal.symbol,
                signal.direction.value,
                signal.style.value,
                str(signal.entry_price),
                str(signal.stop_loss),
                str(signal.take_profit),
                str(signal.confidence),
                signal.reason,
                json.dumps(signal.metadata),
            )

    async def insert_trade(self, plan: Any, mode: str, status: str, metadata: dict[str, Any] | None = None) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trades(symbol, direction, quantity, entry_price, stop_loss, take_profit, mode, status, risk_amount, metadata)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                plan.symbol,
                plan.direction.value,
                str(plan.quantity),
                str(plan.entry_price),
                str(plan.stop_loss),
                str(plan.take_profit),
                mode,
                status,
                str(getattr(plan, "risk_amount", "0")),
                json.dumps(metadata or {}),
            )

    async def has_open_shadow_trade(self, symbol: str, strategy: str) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM shadow_trades
                WHERE symbol=$1 AND strategy=$2 AND status IN ('ACCEPTED', 'OPEN', 'ACTIVE')
                LIMIT 1
                """,
                symbol,
                strategy,
            )
        return row is not None

    async def insert_shadow_trade(
        self,
        *,
        signal: Any,
        strategy: str,
        quantity: str,
        risk_amount: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shadow_trades(
                    symbol, direction, strategy, quantity, entry_price, stop_loss,
                    take_profit, mode, status, risk_amount, metadata
                )
                VALUES($1,$2,$3,$4,$5,$6,$7,'SHADOW_PAPER','OPEN',$8,$9)
                """,
                signal.symbol,
                signal.direction.value,
                strategy,
                quantity,
                str(signal.entry_price),
                str(signal.stop_loss),
                str(signal.take_profit),
                risk_amount,
                json.dumps(metadata or {}),
            )

    async def recent_shadow_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM shadow_trades ORDER BY id DESC LIMIT $1", limit)
        return [dict(row) for row in rows]

    async def recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM trades ORDER BY id DESC LIMIT $1", limit)
        return [dict(row) for row in rows]

    async def mark_latest_trade_closed(self, symbol: str, realized_pnl: str, r_multiple: str | None = None) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            trade = await conn.fetchrow(
                """
                SELECT id, risk_amount FROM trades
                WHERE symbol=$1 AND status != 'CLOSED'
                ORDER BY id DESC
                LIMIT 1
                """,
                symbol,
            )
            if not trade:
                return
            if r_multiple is None:
                r_multiple = _r_multiple(realized_pnl, trade["risk_amount"])
            await conn.execute(
                """
                UPDATE trades
                SET status='CLOSED', realized_pnl=$1, r_multiple=$2, closed_at=now()
                WHERE id=$3
                """,
                realized_pnl,
                r_multiple,
                trade["id"],
            )

    async def sync_live_position(
        self,
        *,
        symbol: str,
        direction: str,
        quantity: str,
        entry_price: str,
        stop_loss: str | None,
        take_profit: str | None,
        mode: str,
        status: str,
        metadata: dict[str, Any],
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            trade = await conn.fetchrow(
                """
                SELECT id FROM trades
                WHERE symbol=$1 AND status != 'CLOSED'
                ORDER BY id DESC
                LIMIT 1
                """,
                symbol,
            )
            payload = json.dumps(metadata or {})
            sl = stop_loss or "0"
            tp = take_profit or "0"
            if trade:
                await conn.execute(
                    """
                    UPDATE trades
                    SET direction=$1, quantity=$2, entry_price=$3, stop_loss=$4, take_profit=$5,
                        mode=$6, status=$7, metadata=$8
                    WHERE id=$9
                    """,
                    direction,
                    quantity,
                    entry_price,
                    sl,
                    tp,
                    mode,
                    status,
                    payload,
                    trade["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO trades(symbol, direction, quantity, entry_price, stop_loss, take_profit, mode, status, risk_amount, metadata)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,'0',$9)
                    """,
                    symbol,
                    direction,
                    quantity,
                    entry_price,
                    sl,
                    tp,
                    mode,
                    status,
                    payload,
                )

    async def close_absent_live_positions(self, open_symbols: set[str], mode: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, symbol FROM trades WHERE mode=$1 AND status != 'CLOSED'", mode)
            stale_ids = [row["id"] for row in rows if row["symbol"] not in open_symbols]
            if stale_ids:
                await conn.execute(
                    "UPDATE trades SET status='CLOSED', closed_at=now() WHERE id=ANY($1::bigint[])",
                    stale_ids,
                )

    async def recent_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM signals ORDER BY id DESC LIMIT $1", limit)
        return [dict(row) for row in rows]

    async def insert_filter_rejection(
        self,
        symbol: str,
        direction: str,
        strategy: str,
        confidence: str,
        filter_type: str,
        reason: str,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO filter_rejections(symbol, direction, strategy, confidence, filter_type, reason)
                VALUES($1,$2,$3,$4,$5,$6)
                """,
                symbol,
                direction,
                strategy,
                confidence,
                filter_type,
                reason,
            )

    async def recent_rejections(self, limit: int = 100) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM filter_rejections ORDER BY id DESC LIMIT $1", limit)
        return [dict(row) for row in rows]

    async def insert_ml_feature_snapshot(
        self,
        *,
        symbol: str,
        direction: str,
        strategy: str,
        confidence: str,
        decision: str,
        reason: str,
        features: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ml_feature_snapshots
                    (symbol, direction, strategy, confidence, decision, reason, features, metadata)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                symbol,
                direction,
                strategy,
                confidence,
                decision,
                reason,
                json.dumps(features),
                json.dumps(metadata),
            )

    async def ml_feature_snapshots(self, limit: int = 10_000) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM ml_feature_snapshots ORDER BY id DESC LIMIT $1", limit)
        return [dict(row) for row in rows]

    async def save_risk_state(
        self,
        losing_streak: int,
        cooldown_until: str | None,
        realized_pnl_today: str,
        pnl_date_utc: str | None = None,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO risk_state (id, losing_streak, cooldown_until, realized_pnl_today, pnl_date_utc, updated_at)
                VALUES (1, $1, $2, $3, $4, now())
                ON CONFLICT(id) DO UPDATE SET
                    losing_streak=EXCLUDED.losing_streak,
                    cooldown_until=EXCLUDED.cooldown_until,
                    realized_pnl_today=EXCLUDED.realized_pnl_today,
                    pnl_date_utc=EXCLUDED.pnl_date_utc,
                    updated_at=EXCLUDED.updated_at
                """,
                losing_streak,
                cooldown_until,
                realized_pnl_today,
                pnl_date_utc,
            )

    async def load_risk_state(self) -> dict | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT losing_streak, cooldown_until, realized_pnl_today, pnl_date_utc, updated_at "
                "FROM risk_state WHERE id=1"
            )
        return dict(row) if row else None

    async def pnl_summary(self, mode: str | None = None) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            if mode:
                rows = await conn.fetch("SELECT realized_pnl FROM trades WHERE mode = $1", mode)
            else:
                rows = await conn.fetch("SELECT realized_pnl FROM trades")
        pnl = sum(float(row["realized_pnl"]) for row in rows)
        return {"realized_pnl": pnl, "trades": len(rows)}

    def _require_pool(self) -> Any:
        if not self._pool:
            raise RuntimeError("PostgreSQL database is not connected.")
        return self._pool


def _r_multiple(realized_pnl: str, risk_amount: Any) -> str:
    try:
        risk = Decimal(str(risk_amount or "0"))
        if risk <= 0:
            return "0"
        return str(Decimal(str(realized_pnl)) / risk)
    except (InvalidOperation, ValueError):
        return "0"
