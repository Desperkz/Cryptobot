from __future__ import annotations

from decimal import Decimal

import trading_bot.strategy_engine.trend_pullback as trend_pullback
from trading_bot.config import StrategyConfig
from trading_bot.models import (
    Candle,
    Direction,
    EdgeSnapshot,
    MarketMetrics,
    MarketRegime,
    RegimeSnapshot,
)
from trading_bot.strategy_engine.trend_pullback import TrendPullbackStrategy


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
        "enabled_strategies": ["SQUEEZE_BREAKOUT"],
        "strategy_modes": {"TREND_PULLBACK": "shadow"},
        "trend_pullback_min_volume_ratio": Decimal("1.10"),
        "trend_pullback_min_trend_strength": Decimal("0.35"),
        "trend_pullback_min_depth_atr": Decimal("0.25"),
        "trend_pullback_max_depth_atr": Decimal("2.20"),
        "trend_pullback_stop_atr_multiplier": Decimal("1.20"),
        "trend_pullback_take_profit_rr": Decimal("1.60"),
        "trend_pullback_min_confluence": 4,
        "trend_pullback_min_edge_score": Decimal("0.25"),
    }
    values.update(overrides)
    return StrategyConfig(**values)


def candles(count: int) -> list[Candle]:
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
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("97"),
        close=Decimal("99"),
        volume=Decimal("100"),
        close_time=result[-2].close_time,
    )
    result[-1] = Candle(
        open_time=result[-1].open_time,
        open=Decimal("99"),
        high=Decimal("103"),
        low=Decimal("98"),
        close=Decimal("102"),
        volume=Decimal("160"),
        close_time=result[-1].close_time,
    )
    return result


class FakeRegimeDetector:
    def __init__(self, regime: MarketRegime = MarketRegime.TREND_UP, trend_strength: Decimal = Decimal("1.4")) -> None:
        self.regime = regime
        self.trend_strength = trend_strength

    def detect(self, _candles):
        return RegimeSnapshot(
            regime=self.regime,
            atr_pct=Decimal("2"),
            trend_strength=self.trend_strength,
            momentum_pct=Decimal("1.2"),
            reason="test trend",
        )


class FakeEdgeAnalyzer:
    def __init__(self, score: Decimal = Decimal("0.32")) -> None:
        self.score = score

    def analyze(self, *_args, **_kwargs) -> EdgeSnapshot:
        return EdgeSnapshot(
            liquidity_sweep=False,
            sweep_direction=Direction.NONE,
            absorption=True,
            absorption_direction=Direction.LONG,
            structure_break=True,
            structure_direction=Direction.LONG,
            liquidation_zone_nearby=False,
            score=self.score,
            reasons=("absorption", "structure_break"),
        )


def metrics(**overrides) -> MarketMetrics:
    values = {
        "symbol": "BTCUSDT",
        "quote_volume_24h": Decimal("100000000"),
        "spread_bps": Decimal("4"),
        "order_book_imbalance": Decimal("0.12"),
        "taker_buy_ratio": Decimal("0.54"),
        "aggressive_buy_sell_delta": Decimal("0.10"),
    }
    values.update(overrides)
    return MarketMetrics(**values)


def patch_indicators(monkeypatch, candles_15m, candles_1h, candles_4h) -> None:
    def fake_ema(values, period):
        if max(values) >= 115:
            return [90.0 if period == 200 else 105.0 if period == 20 else 100.0] * len(values)
        return [100.0] * len(values)

    monkeypatch.setattr(trend_pullback, "ema", fake_ema)
    monkeypatch.setattr(trend_pullback, "atr", lambda items, period: [4.0 if items is candles_1h else 1.0] * len(items))
    monkeypatch.setattr(trend_pullback, "rsi", lambda values, period: [58.0] * len(values))


def test_trend_pullback_shadow_candidate_records_evidence(monkeypatch) -> None:
    candles_15m = candles(220)
    candles_1h = candles(220)
    candles_4h = [Candle(c.open_time, Decimal("120"), Decimal("121"), Decimal("119"), Decimal("120"), c.volume, c.close_time) for c in candles(220)]
    patch_indicators(monkeypatch, candles_15m, candles_1h, candles_4h)

    strategy = TrendPullbackStrategy(
        strategy_config(),
        FakeRegimeDetector(),
        FakeEdgeAnalyzer(),
    )

    signal = strategy.generate("BTCUSDT", candles_15m, candles_1h, candles_4h, metrics())

    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.metadata["strategy"] == "TREND_PULLBACK"
    assert signal.metadata["trend_pullback_confluence"] == "6"
    assert "pullback_depth" in signal.metadata["trend_pullback_flags"]
    assert "edge" in signal.metadata["trend_pullback_flags"]


def test_trend_pullback_blocks_when_order_flow_disagrees(monkeypatch) -> None:
    candles_15m = candles(220)
    candles_1h = candles(220)
    candles_4h = [Candle(c.open_time, Decimal("120"), Decimal("121"), Decimal("119"), Decimal("120"), c.volume, c.close_time) for c in candles(220)]
    patch_indicators(monkeypatch, candles_15m, candles_1h, candles_4h)

    strategy = TrendPullbackStrategy(
        strategy_config(),
        FakeRegimeDetector(),
        FakeEdgeAnalyzer(),
    )

    assert strategy.generate(
        "BTCUSDT",
        candles_15m,
        candles_1h,
        candles_4h,
        metrics(order_book_imbalance=Decimal("-0.05"), taker_buy_ratio=Decimal("0.46")),
    ) is None


def test_trend_pullback_blocks_weak_trend(monkeypatch) -> None:
    candles_15m = candles(220)
    candles_1h = candles(220)
    candles_4h = [Candle(c.open_time, Decimal("120"), Decimal("121"), Decimal("119"), Decimal("120"), c.volume, c.close_time) for c in candles(220)]
    patch_indicators(monkeypatch, candles_15m, candles_1h, candles_4h)

    strategy = TrendPullbackStrategy(
        strategy_config(),
        FakeRegimeDetector(trend_strength=Decimal("0.1")),
        FakeEdgeAnalyzer(),
    )

    assert strategy.generate("BTCUSDT", candles_15m, candles_1h, candles_4h, metrics()) is None
