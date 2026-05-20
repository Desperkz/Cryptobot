from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_bot.bot import TradingBot, _disaster_config_from_app_config, _paper_trade_unrealized_pnl
from trading_bot.models import TradingMode


class FakeDB:
    def __init__(self, trades: list[dict], realized_pnl: str = "0") -> None:
        self.trades = trades
        self.realized_pnl = realized_pnl

    async def pnl_summary(self) -> dict:
        return {"realized_pnl": self.realized_pnl}

    async def recent_trades(self, _limit: int = 50) -> list[dict]:
        return self.trades


class FakeBinance:
    def __init__(self, prices: dict[str, str]) -> None:
        self.prices = prices

    async def ticker_price(self, symbol: str) -> dict:
        return {"price": self.prices[symbol]}


def test_paper_trade_unrealized_pnl_for_long_and_short() -> None:
    long_trade = {
        "direction": "LONG",
        "quantity": "2",
        "entry_price": "100",
    }
    short_trade = {
        "direction": "SHORT",
        "quantity": "3",
        "entry_price": "50",
    }

    assert _paper_trade_unrealized_pnl(long_trade, Decimal("110")) == Decimal("20")
    assert _paper_trade_unrealized_pnl(short_trade, Decimal("45")) == Decimal("15")


@pytest.mark.asyncio
async def test_paper_current_equity_includes_realized_and_unrealized_pnl() -> None:
    bot = object.__new__(TradingBot)
    bot.config = SimpleNamespace(
        is_live=False,
        mode=TradingMode.PAPER_TRADING,
        account=SimpleNamespace(initial_equity_usdt=Decimal("1000")),
    )
    bot.db = FakeDB(
        realized_pnl="10",
        trades=[
            {
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "quantity": "2",
                "entry_price": "100",
                "mode": TradingMode.PAPER_TRADING.value,
                "status": "OPEN",
            },
            {
                "symbol": "ETHUSDT",
                "direction": "SHORT",
                "quantity": "3",
                "entry_price": "50",
                "mode": TradingMode.PAPER_TRADING.value,
                "status": "ACTIVE",
            },
            {
                "symbol": "XRPUSDT",
                "direction": "LONG",
                "quantity": "100",
                "entry_price": "1",
                "mode": TradingMode.PAPER_TRADING.value,
                "status": "CLOSED",
            },
        ],
    )
    bot.binance = FakeBinance({"BTCUSDT": "110", "ETHUSDT": "45"})

    assert await bot.current_equity_usdt() == Decimal("1045")


def test_disaster_config_uses_risk_daily_loss_limit() -> None:
    config = SimpleNamespace(risk=SimpleNamespace(max_daily_loss_pct=Decimal("0.06")))

    disaster_config = _disaster_config_from_app_config(config)

    assert disaster_config.max_daily_loss_pct == 0.06
