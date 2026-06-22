from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from trading_bot.config import StrategyConfig
from trading_bot.models import Candle, MarketMetrics, MarketRegime, RegimeSnapshot, TradingStyle
from trading_bot.strategy_engine.multi_timeframe import MultiTimeframeStrategy


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
        "use_funding_filter": False,
        "max_abs_funding_rate": Decimal("0.0008"),
        "enabled_strategies": ["SQUEEZE_BREAKOUT"],
        "strategy_modes": {"TREND_FOLLOWING": "shadow"},
    }
    values.update(overrides)
    return StrategyConfig(**values)


def candles(count: int, start: Decimal = Decimal("100"), step: Decimal = Decimal("0")) -> list[Candle]:
    result: list[Candle] = []
    for i in range(count):
        close = start + (step * Decimal(i))
        result.append(
            Candle(
                open_time=i * 60_000,
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("200"),
                close_time=(i + 1) * 60_000,
            )
        )
    return result


def metrics() -> MarketMetrics:
    return MarketMetrics(
        symbol="BTCUSDT",
        quote_volume_24h=Decimal("100000000"),
        spread_bps=Decimal("2"),
        order_book_imbalance=Decimal("0.15"),
        taker_buy_ratio=Decimal("0.56"),
        open_interest_change_pct=Decimal("0.4"),
        aggressive_buy_sell_delta=Decimal("0.10"),
    )


def metrics_without_open_interest() -> MarketMetrics:
    return replace(metrics(), open_interest_change_pct=None)


class FakeRegimeDetector:
    def __init__(self, regime: MarketRegime) -> None:
        self.regime = regime

    def detect(self, _candles) -> RegimeSnapshot:
        return RegimeSnapshot(
            regime=self.regime,
            atr_pct=Decimal("2"),
            trend_strength=Decimal("1.2"),
            momentum_pct=Decimal("0.8"),
            reason="test",
        )


class FakeStyleSelector:
    def select(self, *_args, **_kwargs) -> TradingStyle:
        return TradingStyle.INTRADAY


def strategy(regime: MarketRegime) -> MultiTimeframeStrategy:
    return MultiTimeframeStrategy(
        strategy_config(),
        FakeRegimeDetector(regime),
        FakeStyleSelector(),
        edge_filters=None,
    )


def test_trend_following_diagnostic_names_non_trend_regime() -> None:
    signal, diagnostic = strategy(MarketRegime.RANGE).evaluate(
        "BTCUSDT",
        candles(220),
        candles(220),
        candles(220),
        metrics(),
    )

    assert signal is None
    assert diagnostic["block_reason"] == "no_trend_regime"
    assert diagnostic["trend_regime"] == "RANGE"


def test_trend_following_diagnostic_treats_momentum_as_separate_strategy() -> None:
    signal, diagnostic = strategy(MarketRegime.MOMENTUM).evaluate(
        "BTCUSDT",
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        metrics(),
    )

    assert signal is None
    assert diagnostic["block_reason"] == "no_trend_regime"
    assert diagnostic["trend_regime"] == "MOMENTUM"


def test_trend_following_diagnostic_names_missing_4h_alignment() -> None:
    signal, diagnostic = strategy(MarketRegime.TREND_UP).evaluate(
        "BTCUSDT",
        candles(220),
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        candles(220, start=Decimal("120"), step=Decimal("-0.1")),
        metrics(),
    )

    assert signal is None
    assert diagnostic["block_reason"] == "no_4h_bullish_alignment"
    assert diagnostic["bullish_4h_alignment"] is False


def test_trend_following_diagnostic_names_missing_1h_structure() -> None:
    signal, diagnostic = strategy(MarketRegime.TREND_UP).evaluate(
        "BTCUSDT",
        candles(220),
        candles(220),
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        metrics(),
    )

    assert signal is None
    assert diagnostic["block_reason"] in {"no_1h_bullish_ema_stack", "no_1h_higher_high_higher_low"}
    assert "hh_hl" in diagnostic


def test_trend_following_diagnostic_requires_open_interest_confirmation() -> None:
    signal, diagnostic = strategy(MarketRegime.TREND_UP).evaluate(
        "BTCUSDT",
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        candles(220, start=Decimal("100"), step=Decimal("0.2")),
        metrics_without_open_interest(),
    )

    assert signal is None
    assert diagnostic["block_reason"] == "missing_open_interest_confirmation"
