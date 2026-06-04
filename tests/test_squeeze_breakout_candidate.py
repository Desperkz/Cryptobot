from __future__ import annotations

from decimal import Decimal

import trading_bot.strategy_engine.squeeze_breakout as squeeze
from trading_bot.config import StrategyConfig
from trading_bot.models import Candle, MarketMetrics, MarketRegime, RegimeSnapshot, Direction
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy


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
    }
    values.update(overrides)
    return StrategyConfig(**values)


def candles(
    count: int,
    *,
    close: str = "103",
    open_: str = "101.5",
    high: str = "104",
    low: str = "100",
    current_volume: str = "180",
    release_offset: int = 0,
    release_volume: str = "220",
) -> list[Candle]:
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
    if release_offset:
        release_idx = count - 1 - release_offset
        result[release_idx] = Candle(
            open_time=result[release_idx].open_time,
            open=Decimal("100.5"),
            high=Decimal("103"),
            low=Decimal("100"),
            close=Decimal("102"),
            volume=Decimal(release_volume),
            close_time=result[release_idx].close_time,
        )
    result[-1] = Candle(
        open_time=result[-1].open_time,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(current_volume),
        close_time=result[-1].close_time,
    )
    return result


def metrics() -> MarketMetrics:
    return MarketMetrics(
        symbol="BTCUSDT",
        quote_volume_24h=Decimal("100000000"),
        spread_bps=Decimal("3"),
        top_book_liquidity_usdt=Decimal("500000"),
        taker_buy_ratio=Decimal("0.56"),
        aggressive_buy_sell_delta=Decimal("0.12"),
    )


class FakeRegimeDetector:
    def detect(self, _candles):
        return RegimeSnapshot(
            regime=MarketRegime.RANGE,
            atr_pct=Decimal("1"),
            trend_strength=Decimal("0.2"),
            momentum_pct=Decimal("0.1"),
            reason="test range",
        )


def patch_squeeze(monkeypatch, candles_1h: list[Candle], *, release: bool = True, release_offset: int = 0) -> None:
    monkeypatch.setattr(
        squeeze,
        "_detect_squeeze",
        lambda items, *args, **kwargs: (True, 10) if items is candles_1h else (True, 4),
    )
    monkeypatch.setattr(
        squeeze,
        "_recent_squeeze_release",
        lambda *_args, **_kwargs: (release, 10 if release else 0, release_offset if release else 0),
    )
    monkeypatch.setattr(squeeze, "_squeeze_momentum", lambda items, period=20: [0.1, 0.2, 0.35, 0.55, 0.8] * 20)
    monkeypatch.setattr(squeeze, "atr", lambda items, period: [1.0] * len(items))


def test_squeeze_champion_accepts_release_with_retest_confirmation(monkeypatch) -> None:
    candles_1h = candles(80, release_offset=1, release_volume="220")
    patch_squeeze(monkeypatch, candles_1h, release=True, release_offset=1)

    signal = SqueezeBreakoutStrategy(strategy_config(), FakeRegimeDetector()).generate(
        "BTCUSDT",
        candles(80),
        candles_1h,
        candles(80),
        metrics(),
    )

    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.metadata["strategy"] == "SQUEEZE_BREAKOUT"
    assert signal.metadata["squeeze_entry_timing"] == "release_followthrough"
    assert signal.metadata["squeeze_retest_required"] is True
    assert signal.metadata["squeeze_retest_confirmed"] is True
    assert Decimal(signal.metadata["breakout_atr"]) >= Decimal("0.03")
    assert Decimal(signal.metadata["rr"]) == Decimal("2.0")


def test_squeeze_champion_uses_configured_intraday_stop_multiplier(monkeypatch) -> None:
    candles_1h = candles(80, close="103", release_offset=1, release_volume="220")
    patch_squeeze(monkeypatch, candles_1h, release=True, release_offset=1)
    cfg = strategy_config(stop_atr_multiplier={"SCALPING": Decimal("1.2"), "INTRADAY": Decimal("2.2")})

    signal = SqueezeBreakoutStrategy(cfg, FakeRegimeDetector()).generate(
        "BTCUSDT",
        candles(80),
        candles_1h,
        candles(80),
        metrics(),
    )

    assert signal is not None
    assert signal.entry_price - signal.stop_loss == Decimal("2.2")
    assert signal.metadata["stop_atr_multiplier"] == "2.2"


def test_squeeze_champion_blocks_momentum_without_range_breakout(monkeypatch) -> None:
    candles_1h = candles(80, close="100.8", open_="100.2", high="101", low="99.8")
    patch_squeeze(monkeypatch, candles_1h, release=True)

    signal = SqueezeBreakoutStrategy(strategy_config(), FakeRegimeDetector()).generate(
        "BTCUSDT",
        candles(80),
        candles_1h,
        candles(80),
        metrics(),
    )

    assert signal is None


def test_squeeze_champion_uses_release_window_volume_for_followthrough(monkeypatch) -> None:
    candles_1h = candles(80, current_volume="100", release_offset=2, release_volume="230")
    patch_squeeze(monkeypatch, candles_1h, release=True, release_offset=2)

    signal = SqueezeBreakoutStrategy(strategy_config(), FakeRegimeDetector()).generate(
        "BTCUSDT",
        candles(80),
        candles_1h,
        candles(80),
        metrics(),
    )

    assert signal is not None
    assert signal.metadata["squeeze_release_offset"] == 2
    assert signal.metadata["squeeze_retest_required"] is True
    assert signal.metadata["squeeze_retest_confirmed"] is True
    assert Decimal(signal.metadata["volume_ratio"]) >= Decimal("2.0")


def test_squeeze_champion_blocks_late_followthrough_without_retest(monkeypatch) -> None:
    candles_1h = candles(
        80,
        close="103",
        open_="102.4",
        high="104",
        low="102",
        current_volume="100",
        release_offset=2,
        release_volume="230",
    )
    patch_squeeze(monkeypatch, candles_1h, release=True, release_offset=2)

    signal = SqueezeBreakoutStrategy(strategy_config(), FakeRegimeDetector()).generate(
        "BTCUSDT",
        candles(80),
        candles_1h,
        candles(80),
        metrics(),
    )

    assert signal is None


def test_squeeze_champion_early_build_requires_stronger_volume(monkeypatch) -> None:
    candles_1h = candles(80, current_volume="130")
    patch_squeeze(monkeypatch, candles_1h, release=False)
    strategy = SqueezeBreakoutStrategy(strategy_config(), FakeRegimeDetector())

    assert strategy.generate("BTCUSDT", candles(80), candles_1h, candles(80), metrics()) is None

    stronger_volume = candles(80, current_volume="180")
    patch_squeeze(monkeypatch, stronger_volume, release=False)
    signal = strategy.generate("BTCUSDT", candles(80), stronger_volume, candles(80), metrics())

    assert signal is not None
    assert signal.metadata["squeeze_entry_timing"] == "early_breakout"
