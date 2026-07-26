from __future__ import annotations

from typing import Any

from trading_bot.database.postgres import PostgresDatabase
from trading_bot.database.sqlite import Database as SQLiteDatabase


class Database:
    def __init__(self, url: str) -> None:
        if url.startswith("postgresql://") or url.startswith("postgresql+asyncpg://"):
            self._impl = PostgresDatabase(url)
        else:
            self._impl = SQLiteDatabase(url)

    async def connect(self) -> None:
        await self._impl.connect()

    async def close(self) -> None:
        await self._impl.close()

    async def migrate(self) -> None:
        await self._impl.migrate()

    async def insert_signal(self, signal: Any) -> None:
        await self._impl.insert_signal(signal)

    async def insert_trade(self, plan: Any, mode: str, status: str, metadata: dict[str, Any] | None = None) -> None:
        await self._impl.insert_trade(plan, mode, status, metadata)

    async def has_open_shadow_trade(self, symbol: str, strategy: str) -> bool:
        return await self._impl.has_open_shadow_trade(symbol, strategy)

    async def insert_shadow_trade(
        self,
        *,
        signal: Any,
        strategy: str,
        quantity: str,
        risk_amount: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._impl.insert_shadow_trade(
            signal=signal,
            strategy=strategy,
            quantity=quantity,
            risk_amount=risk_amount,
            metadata=metadata,
        )

    async def recent_shadow_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._impl.recent_shadow_trades(limit)

    async def closed_shadow_trades_by_strategies(
        self,
        strategies: list[str],
        closed_after: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._impl.closed_shadow_trades_by_strategies(strategies, closed_after)

    async def load_operational_state(self, key: str) -> str | None:
        return await self._impl.load_operational_state(key)

    async def save_operational_state(self, key: str, value: str) -> None:
        await self._impl.save_operational_state(key, value)

    async def recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._impl.recent_trades(limit)

    async def mark_latest_trade_closed(self, symbol: str, realized_pnl: str, r_multiple: str | None = None) -> None:
        await self._impl.mark_latest_trade_closed(symbol, realized_pnl, r_multiple)

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
        await self._impl.sync_live_position(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            mode=mode,
            status=status,
            metadata=metadata,
        )

    async def close_absent_live_positions(self, open_symbols: set[str], mode: str) -> None:
        await self._impl.close_absent_live_positions(open_symbols, mode)

    async def recent_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._impl.recent_signals(limit)

    async def insert_filter_rejection(
        self,
        symbol: str,
        direction: str,
        strategy: str,
        confidence: str,
        filter_type: str,
        reason: str,
    ) -> None:
        await self._impl.insert_filter_rejection(symbol, direction, strategy, confidence, filter_type, reason)

    async def recent_rejections(self, limit: int = 100) -> list[dict]:
        return await self._impl.recent_rejections(limit)

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
        await self._impl.insert_ml_feature_snapshot(
            symbol=symbol,
            direction=direction,
            strategy=strategy,
            confidence=confidence,
            decision=decision,
            reason=reason,
            features=features,
            metadata=metadata,
        )

    async def ml_feature_snapshots(self, limit: int = 10_000) -> list[dict[str, Any]]:
        return await self._impl.ml_feature_snapshots(limit)

    async def save_risk_state(
        self,
        losing_streak: int,
        cooldown_until: str | None,
        realized_pnl_today: str,
        pnl_date_utc: str | None = None,
    ) -> None:
        await self._impl.save_risk_state(losing_streak, cooldown_until, realized_pnl_today, pnl_date_utc)

    async def load_risk_state(self) -> dict | None:
        return await self._impl.load_risk_state()

    async def pnl_summary(self, mode: str | None = None) -> dict[str, Any]:
        return await self._impl.pnl_summary(mode)
