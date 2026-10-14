from decimal import Decimal

import pytest

from trading_bot.data_provider.market_data import MarketDataProvider
from trading_bot.execution.entry_pricing import rebase_signal_to_market
from trading_bot.models import Direction, Signal, TradingStyle


def _signal(direction: Direction, entry: str, stop: str, take: str) -> Signal:
    return Signal(
        symbol="SOLUSDT",
        direction=direction,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal(entry),
        stop_loss=Decimal(stop),
        take_profit=Decimal(take),
        confidence=Decimal("0.7"),
        reason="test",
        metadata={"strategy": "SQUEEZE_BREAKOUT"},
    )


def test_long_rebase_keeps_distances_at_market_price():
    signal = _signal(Direction.LONG, "100", "98", "104")
    rebased, reason = rebase_signal_to_market(signal, Decimal("100.5"), Decimal("0.35"))
    assert reason is None
    assert rebased.entry_price == Decimal("100.5")
    assert rebased.stop_loss == Decimal("98.5")
    assert rebased.take_profit == Decimal("104.5")
    assert rebased.metadata["entry_rebase"]["signal_entry"] == "100"
    assert rebased.metadata["entry_rebase"]["drift_r"] == "0.2500"
    # original signal untouched
    assert signal.entry_price == Decimal("100")
    assert "entry_rebase" not in signal.metadata


def test_short_rebase_mirrors_long():
    signal = _signal(Direction.SHORT, "100", "102", "96")
    rebased, reason = rebase_signal_to_market(signal, Decimal("99.6"), Decimal("0.35"))
    assert reason is None
    assert rebased.stop_loss == Decimal("101.6")
    assert rebased.take_profit == Decimal("95.6")
    assert rebased.metadata["entry_rebase"]["drift_r"] == "0.2000"


@pytest.mark.parametrize("price", ["101", "99"])
def test_large_drift_is_rejected_both_ways(price):
    signal = _signal(Direction.LONG, "100", "98", "104")
    rebased, reason = rebase_signal_to_market(signal, Decimal(price), Decimal("0.35"))
    assert rebased is None
    assert "0.50R" in reason


def test_missing_price_fails_closed():
    signal = _signal(Direction.LONG, "100", "98", "104")
    rebased, reason = rebase_signal_to_market(signal, None, Decimal("0.35"))
    assert rebased is None
    assert "unavailable" in reason


def test_zero_drift_limit_disables_guard():
    signal = _signal(Direction.LONG, "100", "98", "104")
    rebased, reason = rebase_signal_to_market(signal, Decimal("103"), Decimal("0"))
    assert reason is None
    assert rebased.stop_loss == Decimal("101")


class _Book:
    def __init__(self, payload):
        self.payload = payload

    async def book_ticker(self, symbol):
        return self.payload


async def test_current_mid_price_from_book_ticker():
    provider = MarketDataProvider(_Book({"bidPrice": "99.9", "askPrice": "100.1"}))
    assert await provider.current_mid_price("SOLUSDT") == Decimal("100.0")


async def test_current_mid_price_rejects_crossed_book():
    provider = MarketDataProvider(_Book({"bidPrice": "101", "askPrice": "100"}))
    assert await provider.current_mid_price("SOLUSDT") is None


def test_paper_reads_mainnet_public_data_live_keeps_venue(tmp_path):
    from dataclasses import replace as dc_replace
    from pathlib import Path

    from trading_bot.config import load_config
    from trading_bot.models import TradingMode

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.mode == TradingMode.PAPER_TRADING
    assert cfg.market_data_base_url == "https://fapi.binance.com"
    testnet = dc_replace(cfg, mode=TradingMode.TESTNET_LIVE)
    assert testnet.market_data_base_url == testnet.exchange.testnet_base_url
