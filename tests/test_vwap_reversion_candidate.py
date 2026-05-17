from __future__ import annotations

from decimal import Decimal

import trading_bot.strategy_engine.candidate_strategies as candidates
from trading_bot.config import StrategyConfig
from trading_bot.models import Candle, Direction, MarketMetrics
from trading_bot.strategy_engine.candidate_strategies import VwapReversionStrategy


def strategy_config(**overrides) -> StrategyConfig:
    values = {
        "ema_fast": 20,
        "ema_mid": 50,
        "ema_slow": 200,
        "rsi_period": 14,
        "atr_period": 14,
        "volume_lookback": 20,
        "min_volume_ratio": Decimal("1.2"),
        "min_atr_pct": Decimal("0.15"),
        "max_atr_pct": Decimal("8.0"),
        "stop_atr_multiplier": {"SCALPING": Decimal("1.2"), "INTRADAY": Decimal("1.8")},
        "take_profit_rr": {"SCALPING": Decimal("1.4"), "INTRADAY": Decimal("1.8")},
        "use_funding_filter": True,
        "max_abs_funding_rate": Decimal("0.0008"),
        "strategy_modes": {"VWAP_REVERSION": "shadow"},
        "vwap_reversion_lookback": 96,
        "vwap_reversion_deviation_atr": Decimal("2.20"),
        "vwap_reversion_max_deviation_atr": Decimal("6.00"),
        "vwap_reversion_min_volume_ratio": Decimal("1.20"),
        "vwap_reversion_stop_atr_multiplier": Decimal("1.00"),
        "vwap_reversion_take_profit_rr": Decimal("1.15"),
    }
    values.update(overrides)
    return StrategyConfig(**values)


def candles(count: int, *, previous_close: str = "96", open_: str = "95", close: str = "96.5") -> list[Candle]:
    result: list[Candle] = []
    for i in range(count):
        result.append(
            Candle(
                open_time=i * 60_000,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("100"),
                close_time=(i + 1) * 60_000,
            )
        )
    result[-2] = Candle(
        open_time=result[-2].open_time,
        open=Decimal("97"),
        high=Decimal("98"),
        low=Decimal("95"),
        close=Decimal(previous_close),
        volume=Decimal("130"),
        close_time=result[-2].close_time,
    )
    result[-1] = Candle(
        open_time=result[-1].open_time,
        open=Decimal(open_),
        high=Decimal("97"),
        low=Decimal("93"),
        close=Decimal(close),
        volume=Decimal("150"),
        close_time=result[-1].close_time,
    )
    return result


def metrics(**overrides) -> MarketMetrics:
    values = {
        "symbol": "BTCUSDT",
        "quote_volume_24h": Decimal("100000000"),
        "spread_bps": Decimal("3"),
        "order_book_imbalance": Decimal("0.04"),
        "taker_buy_ratio": Decimal("0.55"),
        "aggressive_buy_sell_delta": Decimal("0.08"),
    }
    values.update(overrides)
    return MarketMetrics(**values)


def patch_indicators(monkeypatch, *, rsi_value: float = 30.0, volume_ratio: Decimal = Decimal("1.30")) -> None:
    monkeypatch.setattr(candidates, "atr", lambda items, period: [1.0] * len(items))
    monkeypatch.setattr(candidates, "rsi", lambda values, period: [rsi_value] * len(values))
    monkeypatch.setattr(candidates, "_volume_ratio", lambda *_args, **_kwargs: volume_ratio)


def test_vwap_reversion_waits_for_reversal_and_flow_confirmation(monkeypatch) -> None:
    candles_15m = candles(120, previous_close="96", open_="95", close="96.5")
    patch_indicators(monkeypatch)

    signal = VwapReversionStrategy(strategy_config()).generate(
        "BTCUSDT",
        candles_15m,
        candles(120),
        candles(120),
        metrics(),
    )

    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.metadata["strategy"] == "VWAP_REVERSION"
    assert signal.metadata["reversal_confirmed"] == "True"
    assert signal.metadata["flow_confirmed"] == "True"


def test_vwap_reversion_blocks_falling_knife_without_reversal(monkeypatch) -> None:
    candles_15m = candles(120, previous_close="96", open_="96", close="95")
    patch_indicators(monkeypatch)

    strategy = VwapReversionStrategy(strategy_config())
    signal, diagnostic = strategy.evaluate(
        "BTCUSDT",
        candles_15m,
        candles(120),
        candles(120),
        metrics(),
    )

    assert signal is None
    assert diagnostic["block_reason"] == "no_reversal_confirmation"


def test_vwap_reversion_requires_progress_back_toward_vwap(monkeypatch) -> None:
    candles_15m = candles(120, previous_close="96.3", open_="96.0", close="96.1")
    patch_indicators(monkeypatch)

    strategy = VwapReversionStrategy(strategy_config())
    signal, diagnostic = strategy.evaluate(
        "BTCUSDT",
        candles_15m,
        candles(120),
        candles(120),
        metrics(),
    )

    assert signal is None
    assert diagnostic["block_reason"] == "no_vwap_reversion_progress"


def test_vwap_reversion_watch_uses_relaxed_thresholds(monkeypatch) -> None:
    candles_15m = candles(120, previous_close="97.5", open_="97.2", close="98.0")
    patch_indicators(monkeypatch)
    strategy = VwapReversionStrategy(strategy_config())

    assert strategy.generate("BTCUSDT", candles_15m, candles(120), candles(120), metrics()) is None

    watch = strategy.generate_watch("BTCUSDT", candles_15m, candles(120), candles(120), metrics())
    assert watch is not None
    assert watch.metadata["strategy"] == "VWAP_REVERSION_WATCH"
    assert watch.metadata["vwap_variant"] == "watch"


def test_vwap_reversion_blocks_extreme_deviation_and_weak_flow(monkeypatch) -> None:
    candles_15m = candles(120, previous_close="91", open_="90.5", close="91.5")
    patch_indicators(monkeypatch)
    strategy = VwapReversionStrategy(strategy_config())

    assert strategy.generate("BTCUSDT", candles_15m, candles(120), candles(120), metrics()) is None

    controlled_deviation = candles(120, previous_close="96", open_="95", close="96.5")
    assert strategy.generate(
        "BTCUSDT",
        controlled_deviation,
        candles(120),
        candles(120),
        metrics(taker_buy_ratio=Decimal("0.42")),
    ) is None
