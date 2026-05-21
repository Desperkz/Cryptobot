from __future__ import annotations

from dataclasses import replace

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import load_config
from trading_bot.execution.reconciler import ExecutionReconciler, restart_recovery_evidence
from trading_bot.models import TradingMode
from trading_bot.position_manager import PositionManager


def test_restart_recovery_evidence_marks_protected_bot_position() -> None:
    evidence = restart_recovery_evidence(
        {"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100", "markPrice": "101"},
        [
            {"symbol": "BTCUSDT", "type": "STOP_MARKET", "closePosition": "true", "clientOrderId": "bot-BTC-1-sl0"},
            {
                "symbol": "BTCUSDT",
                "type": "TAKE_PROFIT_MARKET",
                "reduceOnly": "true",
                "clientOrderId": "bot-BTC-1-tp1",
            },
        ],
    )

    assert evidence["protected"] is True
    assert evidence["managed_by_bot"] is True
    assert evidence["stop_close_position"] is True
    assert evidence["all_take_profits_reduce_only"] is True


@pytest.mark.asyncio
async def test_reconciler_flags_unprotected_restart_position() -> None:
    binance = FakeBinance(
        positions=[{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100", "markPrice": "101"}],
        orders=[{"symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "reduceOnly": "false"}],
    )
    reconciler = ExecutionReconciler(replace(load_config(), mode=TradingMode.TESTNET_LIVE), binance)

    issues = await reconciler.reconcile()

    messages = [issue.message for issue in issues]
    assert "Active position has no STOP_MARKET/TRAILING stop." in messages
    assert "Active position has take-profit orders that are not reduce-only." in messages


@pytest.mark.asyncio
async def test_live_sync_stores_restart_recovery_evidence_and_warns_when_unprotected() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    bot = TradingBot(config)
    fake_binance = FakeBinance(
        positions=[
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.5",
                "entryPrice": "100",
                "markPrice": "101",
                "liquidationPrice": "50",
                "unRealizedProfit": "0.5",
            }
        ],
        orders=[{"symbol": "BTCUSDT", "type": "TAKE_PROFIT_MARKET", "reduceOnly": "false"}],
    )
    db = FakeDatabase()
    telegram = FakeTelegram()
    bot.binance = fake_binance
    bot.positions = PositionManager(fake_binance)
    bot.db = db
    bot.telegram = telegram

    await bot._sync_live_positions_from_exchange(required=True)

    assert db.synced[0]["symbol"] == "BTCUSDT"
    evidence = db.synced[0]["metadata"]["restart_recovery"]
    assert evidence["protected"] is False
    assert evidence["has_take_profit"] is True
    assert evidence["all_take_profits_reduce_only"] is False
    assert telegram.warnings == [
        "BTCUSDT: restart recovery found active position without verified protective SL/TP."
    ]


class FakeBinance:
    def __init__(self, *, positions: list[dict], orders: list[dict]) -> None:
        self.positions = positions
        self.orders = orders

    async def position_risk(self) -> list[dict]:
        return self.positions

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        if symbol is None:
            return self.orders
        return [order for order in self.orders if order["symbol"] == symbol]


class FakeDatabase:
    def __init__(self) -> None:
        self.synced: list[dict] = []
        self.closed_open_symbols: set[str] | None = None

    async def sync_live_position(self, **kwargs) -> None:
        self.synced.append(kwargs)

    async def close_absent_live_positions(self, open_symbols: set[str], mode: str) -> None:
        self.closed_open_symbols = open_symbols


class FakeTelegram:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    async def risk_warning(self, message: str) -> None:
        self.warnings.append(message)
