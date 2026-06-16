from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import aiosqlite


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.path = self._sqlite_path(url)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self.migrate()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def migrate(self) -> None:
        conn = self._require_conn()
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                style TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                stop_loss TEXT NOT NULL,
                take_profit TEXT NOT NULL,
                confidence TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
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
                closed_at TEXT,
                metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
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
                metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                asset TEXT NOT NULL,
                balance TEXT NOT NULL,
                available_balance TEXT NOT NULL,
                metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata TEXT NOT NULL
            );
            """
        )
        await self._ensure_column("trades", "risk_amount", "TEXT DEFAULT '0'")
        await self._ensure_column("trades", "r_multiple", "TEXT DEFAULT '0'")
        await self._ensure_column("trades", "closed_at", "TEXT")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                losing_streak INTEGER DEFAULT 0,
                cooldown_until TEXT,
                realized_pnl_today TEXT DEFAULT '0',
                pnl_date_utc TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._ensure_column("risk_state", "pnl_date_utc", "TEXT")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS filter_rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                strategy TEXT NOT NULL,
                confidence TEXT NOT NULL,
                filter_type TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ml_feature_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                strategy TEXT NOT NULL,
                confidence TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                features TEXT NOT NULL,
                metadata TEXT NOT NULL
            )
        """)
        await conn.commit()

    async def insert_signal(self, signal: Any) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO signals(symbol, direction, style, entry_price, stop_loss, take_profit, confidence, reason, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.symbol,
                signal.direction.value,
                signal.style.value,
                str(signal.entry_price),
                str(signal.stop_loss),
                str(signal.take_profit),
                str(signal.confidence),
                signal.reason,
                json.dumps(signal.metadata, ensure_ascii=False),
            ),
        )
        await conn.commit()

    async def insert_trade(self, plan: Any, mode: str, status: str, metadata: dict[str, Any] | None = None) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO trades(symbol, direction, quantity, entry_price, stop_loss, take_profit, mode, status, risk_amount, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.symbol,
                plan.direction.value,
                str(plan.quantity),
                str(plan.entry_price),
                str(plan.stop_loss),
                str(plan.take_profit),
                mode,
                status,
                str(getattr(plan, "risk_amount", "0")),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        await conn.commit()

    async def has_open_shadow_trade(self, symbol: str, strategy: str) -> bool:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT 1 FROM shadow_trades
            WHERE symbol=? AND strategy=? AND status IN ('ACCEPTED', 'OPEN', 'ACTIVE')
            LIMIT 1
            """,
            (symbol, strategy),
        ) as cursor:
            row = await cursor.fetchone()
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
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO shadow_trades(
                symbol, direction, strategy, quantity, entry_price, stop_loss,
                take_profit, mode, status, risk_amount, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'SHADOW_PAPER', 'OPEN', ?, ?)
            """,
            (
                signal.symbol,
                signal.direction.value,
                strategy,
                quantity,
                str(signal.entry_price),
                str(signal.stop_loss),
                str(signal.take_profit),
                risk_amount,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        await conn.commit()

    async def recent_shadow_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM shadow_trades ORDER BY id DESC LIMIT ?", (limit,))

    async def recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    async def mark_latest_trade_closed(self, symbol: str, realized_pnl: str, r_multiple: str | None = None) -> None:
        conn = self._require_conn()
        trade = await self._latest_open_trade(symbol)
        if not trade:
            return
        if r_multiple is None:
            r_multiple = _r_multiple(realized_pnl, trade.get("risk_amount"))
        await conn.execute(
            """
            UPDATE trades
            SET status='CLOSED', realized_pnl=?, r_multiple=?, closed_at=CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (realized_pnl, r_multiple, trade["id"]),
        )
        await conn.commit()

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
        conn = self._require_conn()
        trade = await self._latest_open_trade(symbol)
        payload = json.dumps(metadata, ensure_ascii=False)
        sl = stop_loss or "0"
        tp = take_profit or "0"
        if trade:
            await conn.execute(
                """
                UPDATE trades
                SET direction=?, quantity=?, entry_price=?, stop_loss=?, take_profit=?,
                    mode=?, status=?, metadata=?
                WHERE id=?
                """,
                (direction, quantity, entry_price, sl, tp, mode, status, payload, trade["id"]),
            )
        else:
            await conn.execute(
                """
                INSERT INTO trades(symbol, direction, quantity, entry_price, stop_loss, take_profit, mode, status, risk_amount, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '0', ?)
                """,
                (symbol, direction, quantity, entry_price, sl, tp, mode, status, payload),
            )
        await conn.commit()

    async def close_absent_live_positions(self, open_symbols: set[str], mode: str) -> None:
        conn = self._require_conn()
        rows = await self._fetch_all(
            "SELECT id, symbol FROM trades WHERE mode=? AND status != 'CLOSED'",
            (mode,),
        )
        stale_ids = [row["id"] for row in rows if row["symbol"] not in open_symbols]
        if not stale_ids:
            return
        placeholders = ",".join("?" for _ in stale_ids)
        await conn.execute(
            f"UPDATE trades SET status='CLOSED', closed_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            tuple(stale_ids),
        )
        await conn.commit()

    async def save_risk_state(
        self,
        losing_streak: int,
        cooldown_until: str | None,
        realized_pnl_today: str,
        pnl_date_utc: str | None = None,
    ) -> None:
        conn = self._require_conn()
        await conn.execute("""
            INSERT INTO risk_state (id, losing_streak, cooldown_until, realized_pnl_today, pnl_date_utc, updated_at)
            VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                losing_streak=excluded.losing_streak,
                cooldown_until=excluded.cooldown_until,
                realized_pnl_today=excluded.realized_pnl_today,
                pnl_date_utc=excluded.pnl_date_utc,
                updated_at=excluded.updated_at
        """, (losing_streak, cooldown_until, realized_pnl_today, pnl_date_utc))
        await conn.commit()

    async def load_risk_state(self) -> dict | None:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT losing_streak, cooldown_until, realized_pnl_today, pnl_date_utc, updated_at "
            "FROM risk_state WHERE id=1"
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {
                    "losing_streak": row[0],
                    "cooldown_until": row[1],
                    "realized_pnl_today": row[2],
                    "pnl_date_utc": row[3],
                    "updated_at": row[4],
                }
            return None

    async def insert_filter_rejection(
        self,
        symbol: str,
        direction: str,
        strategy: str,
        confidence: str,
        filter_type: str,
        reason: str,
    ) -> None:
        conn = self._require_conn()
        await conn.execute(
            """INSERT INTO filter_rejections
               (symbol, direction, strategy, confidence, filter_type, reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, direction, strategy, confidence, filter_type, reason),
        )
        await conn.commit()

    async def recent_rejections(self, limit: int = 100) -> list[dict]:
        conn = self._require_conn()
        async with conn.execute(
            """SELECT symbol, direction, strategy, confidence, filter_type, reason, created_at
               FROM filter_rejections ORDER BY id DESC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]

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
        conn = self._require_conn()
        await conn.execute(
            """INSERT INTO ml_feature_snapshots
               (symbol, direction, strategy, confidence, decision, reason, features, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol,
                direction,
                strategy,
                confidence,
                decision,
                reason,
                json.dumps(features, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        await conn.commit()

    async def ml_feature_snapshots(self, limit: int = 10_000) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM ml_feature_snapshots ORDER BY id DESC LIMIT ?", (limit,))

    async def recent_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetch_all("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))

    async def pnl_summary(self, mode: str | None = None) -> dict[str, Any]:
        if mode:
            rows = await self._fetch_all("SELECT realized_pnl FROM trades WHERE mode = ?", (mode,))
        else:
            rows = await self._fetch_all("SELECT realized_pnl FROM trades")
        pnl = sum(float(row["realized_pnl"]) for row in rows)
        return {"realized_pnl": pnl, "trades": len(rows)}

    async def _fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self._require_conn()
        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _latest_open_trade(self, symbol: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, risk_amount FROM trades
            WHERE symbol=? AND status != 'CLOSED'
            ORDER BY id DESC
            LIMIT 1
            """,
            (symbol,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    def _require_conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database is not connected.")
        return self._conn

    async def _ensure_column(self, table: str, column: str, definition: str) -> None:
        conn = self._require_conn()
        async with conn.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        if column not in {row["name"] for row in rows}:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _sqlite_path(url: str) -> Path:
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            raise ValueError("Only sqlite+aiosqlite URLs are supported by this starter database module.")
        return Path(url.removeprefix(prefix))


def _r_multiple(realized_pnl: str, risk_amount: Any) -> str:
    try:
        risk = Decimal(str(risk_amount or "0"))
        if risk <= 0:
            return "0"
        return str(Decimal(str(realized_pnl)) / risk)
    except (InvalidOperation, ValueError):
        return "0"
