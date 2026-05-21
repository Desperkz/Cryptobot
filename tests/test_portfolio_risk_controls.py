from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import ConfigError, load_config
from trading_bot.models import Candle, Direction, Position, TradingMode
from trading_bot.risk_manager import CorrelationFilter


def _candles(closes: list[Decimal]) -> list[Candle]:
    return [
        Candle(
            open_time=index,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("1"),
            close_time=index,
        )
        for index, close in enumerate(closes)
    ]


def test_correlation_filter_blocks_same_direction_positive_beta() -> None:
    filt = CorrelationFilter(threshold=0.70, lookback=12)
    filt.update("BTCUSDT", _candles([Decimal(100 + index) for index in range(20)]))
    filt.update("ETHUSDT", _candles([Decimal(200 + index * 2) for index in range(20)]))

    allowed, reason = filt.allow_entry(
        "BTCUSDT",
        Direction.LONG,
        [Position("ETHUSDT", Direction.LONG, Decimal("1"), Decimal("200"))],
    )

    assert allowed is False
    assert "Corr(BTCUSDT,ETHUSDT)" in reason


@pytest.mark.asyncio
async def test_live_realtime_correlation_blocks_when_active_symbol_refresh_fails() -> None:
    config = replace(load_config(), mode=TradingMode.TESTNET_LIVE)
    bot = TradingBot(config)
    bot.market_data = FailingMarketData()

    reason = await bot._refresh_realtime_correlation(
        "BTCUSDT",
        [Position("ETHUSDT", Direction.LONG, Decimal("1"), Decimal("200"))],
    )

    assert reason == "Realtime correlation check unavailable for active positions ETHUSDT; live entry blocked."


def test_runtime_config_uses_live_portfolio_safety_profile() -> None:
    config = load_config()

    assert config.risk.symbol_cooldown_after_loss_minutes >= 180
    assert config.risk.strategy_reentry_cooldown_minutes >= 180
    assert config.risk.scale_in_enabled is False
    assert config.risk.realtime_correlation_enabled is True
    assert config.risk.block_live_when_correlation_unavailable is True
    assert Decimal("0") < config.risk.realtime_correlation_threshold <= Decimal("1")
    assert config.risk.realtime_correlation_lookback >= 10


def test_invalid_realtime_correlation_threshold_is_rejected() -> None:
    config = replace(load_config(), risk=replace(load_config().risk, realtime_correlation_threshold=Decimal("1.5")))

    with pytest.raises(ConfigError, match="realtime_correlation_threshold"):
        config.validate()


class FailingMarketData:
    async def candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        raise RuntimeError("network down")
