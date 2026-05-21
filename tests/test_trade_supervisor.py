from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_bot.config import load_config
from trading_bot.models import Direction, Position, RiskPlan, TakeProfitTarget, TradingMode
from trading_bot.position_manager import PositionManager
from trading_bot.trade_manager.protection import ProtectionManager
from trading_bot.trade_manager.supervisor import TradeSupervisor


@pytest.mark.asyncio
async def test_protective_exit_cancels_remaining_orders_and_clears_state() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    binance = FakeBinance()
    positions = PositionManager()
    positions.set_local_position(
        Position(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            managed_by_bot=True,
        )
    )
    supervisor = TradeSupervisor(config, binance, positions, ProtectionManager(binance))
    supervisor.register_plan(_risk_plan())
    warnings: list[str] = []

    async def warn(message: str) -> None:
        warnings.append(message)

    supervisor.set_warning_callback(warn)
    await supervisor.handle_user_stream_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "s": "BTCUSDT",
                "X": "FILLED",
                "x": "TRADE",
                "o": "STOP_MARKET",
                "ot": "STOP_MARKET",
                "c": "bot-BTCUSDT-1-sl0",
                "rp": "-5",
                "z": "1",
                "l": "1",
            },
        }
    )

    assert binance.cancelled_symbols == ["BTCUSDT"]
    assert await positions.active_positions() == []
    assert "protective exit filled" in warnings[-1]


@pytest.mark.asyncio
async def test_account_update_position_close_cancels_stale_reduce_only_orders() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    binance = FakeBinance()
    positions = PositionManager()
    positions.set_local_position(
        Position(
            symbol="ETHUSDT",
            direction=Direction.SHORT,
            quantity=Decimal("2"),
            entry_price=Decimal("100"),
            managed_by_bot=True,
        )
    )
    supervisor = TradeSupervisor(config, binance, positions, ProtectionManager(binance))
    supervisor.register_plan(_risk_plan(symbol="ETHUSDT", direction=Direction.SHORT))

    await supervisor.handle_user_stream_event(
        {
            "e": "ACCOUNT_UPDATE",
            "a": {
                "m": "ORDER",
                "P": [{"s": "ETHUSDT", "pa": "0", "ep": "100", "up": "0"}],
            },
        }
    )

    assert binance.cancelled_symbols == ["ETHUSDT"]
    assert await positions.active_positions() == []


def _risk_plan(symbol: str = "BTCUSDT", direction: Direction = Direction.LONG) -> RiskPlan:
    return RiskPlan(
        symbol=symbol,
        direction=direction,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95") if direction == Direction.LONG else Decimal("105"),
        take_profit=Decimal("110") if direction == Direction.LONG else Decimal("90"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        initial_margin=Decimal("50"),
        risk_amount=Decimal("5"),
        reward_amount=Decimal("10"),
        risk_pct=Decimal("0.01"),
        leverage=2,
        reward_risk=Decimal("2"),
        partial_take_profits=(
            TakeProfitTarget("TP1", Decimal("105"), Decimal("0.4"), Decimal("0.4"), Decimal("1")),
            TakeProfitTarget("TP2", Decimal("108"), Decimal("0.35"), Decimal("0.35"), Decimal("1.6")),
            TakeProfitTarget("RUNNER", Decimal("110"), Decimal("0.25"), Decimal("0.25"), Decimal("2")),
        ),
    )


class FakeBinance:
    def __init__(self) -> None:
        self.cancelled_symbols: list[str] = []

    async def cancel_all_orders(self, symbol: str) -> dict:
        self.cancelled_symbols.append(symbol)
        return {"symbol": symbol}
