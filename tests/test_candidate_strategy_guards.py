from __future__ import annotations

from decimal import Decimal

import trading_bot.strategy_engine.candidate_strategies as candidates
from trading_bot.config import StrategyConfig
from trading_bot.models import Candle, Direction, EdgeSnapshot, MarketMetrics, MarketRegime, RegimeSnapshot
from trading_bot.strategy_engine.candidate_strategies import (
    LiquiditySweepReversalStrategy,
    MomentumContinuationStrategy,
    RangeGridStrategy,
)


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
        "liquidity_sweep_lookback": 18,
        "liquidity_sweep_stop_atr_multiplier": Decimal("1.35"),
        "liquidity_sweep_take_profit_rr": Decimal("1.55"),
        "liquidity_sweep_min_edge_score": Decimal("0.65"),
        "liquidity_sweep_min_reclaim_atr": Decimal("0.90"),
        "liquidity_sweep_follow_through_min_body_atr": Decimal("0.25"),
        "momentum_continuation_min_volume_ratio": Decimal("1.80"),
        "momentum_continuation_stop_atr_multiplier": Decimal("1.60"),
        "momentum_continuation_take_profit_rr": Decimal("1.30"),
        "momentum_continuation_min_edge_score": Decimal("0.60"),
        "range_grid_take_profit_rr": Decimal("1.40"),
        "range_grid_entry_zone_pct": Decimal("0.10"),
        "range_grid_rsi_long_max": Decimal("35"),
        "range_grid_rsi_short_min": Decimal("65"),
    }
    values.update(overrides)
    return StrategyConfig(**values)


def flat_candles(count: int) -> list[Candle]:
    return [
        Candle(
            open_time=i * 60_000,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("100"),
            close_time=(i + 1) * 60_000,
        )
        for i in range(count)
    ]


def metrics(**overrides) -> MarketMetrics:
    values = {
        "symbol": "BTCUSDT",
        "quote_volume_24h": Decimal("100000000"),
        "spread_bps": Decimal("3"),
        "order_book_imbalance": Decimal("0.08"),
        "taker_buy_ratio": Decimal("0.56"),
        "aggressive_buy_sell_delta": Decimal("0.14"),
    }
    values.update(overrides)
    return MarketMetrics(**values)


class FakeEdgeAnalyzer:
    def __init__(self, *, score: Decimal = Decimal("0.72"), reasons: tuple[str, ...] | None = None) -> None:
        self.score = score
        self.reasons = reasons or ("liquidity_sweep", "absorption", "aggressive_flow")

    def analyze(self, *_args, **_kwargs) -> EdgeSnapshot:
        return EdgeSnapshot(
            liquidity_sweep="liquidity_sweep" in self.reasons,
            sweep_direction=Direction.LONG,
            absorption="absorption" in self.reasons,
            absorption_direction=Direction.LONG,
            structure_break="structure_break" in self.reasons,
            structure_direction=Direction.LONG,
            liquidation_zone_nearby=False,
            score=self.score,
            reasons=self.reasons,
        )


class FakeRegimeDetector:
    def detect(self, _candles) -> RegimeSnapshot:
        return RegimeSnapshot(
            regime=MarketRegime.TREND_UP,
            atr_pct=Decimal("2"),
            trend_strength=Decimal("8"),
            momentum_pct=Decimal("1.5"),
            reason="test trend",
        )


class FakeRangeRegimeDetector:
    def detect(self, _candles) -> RegimeSnapshot:
        return RegimeSnapshot(
            regime=MarketRegime.RANGE,
            atr_pct=Decimal("1.5"),
            trend_strength=Decimal("0.1"),
            momentum_pct=Decimal("0.0"),
            reason="test range",
        )


def test_liquidity_sweep_requires_strict_edge_and_flow(monkeypatch) -> None:
    candles = flat_candles(60)
    candles[-1] = Candle(
        open_time=candles[-1].open_time,
        open=Decimal("99.2"),
        high=Decimal("100.6"),
        low=Decimal("98.2"),
        close=Decimal("100.1"),
        volume=Decimal("160"),
        close_time=candles[-1].close_time,
    )
    monkeypatch.setattr(candidates, "atr", lambda items, period: [1.0] * len(items))
    monkeypatch.setattr(candidates, "_volume_ratio", lambda *_args, **_kwargs: Decimal("1.35"))

    strategy = LiquiditySweepReversalStrategy(strategy_config(), FakeEdgeAnalyzer())
    signal = strategy.generate("BTCUSDT", candles, candles, candles, metrics())

    assert signal is not None
    assert signal.metadata["strategy"] == "LIQUIDITY_SWEEP_REVERSAL"
    assert signal.metadata["hour_utc"] == "1"
    assert Decimal(signal.metadata["reclaim_atr"]) >= Decimal("0.90")
    assert signal.metadata["follow_through_confirmed"] == "True"

    assert strategy.generate(
        "BTCUSDT",
        candles,
        candles,
        candles,
        metrics(taker_buy_ratio=Decimal("0.49")),
    ) is None

    weak_edge = LiquiditySweepReversalStrategy(
        strategy_config(),
        FakeEdgeAnalyzer(reasons=("liquidity_sweep", "aggressive_flow")),
    )
    assert weak_edge.generate("BTCUSDT", candles, candles, candles, metrics()) is None

    weak_follow_through = list(candles)
    weak_follow_through[-1] = Candle(
        open_time=candles[-1].open_time,
        open=Decimal("99.2"),
        high=Decimal("100.0"),
        low=Decimal("98.2"),
        close=Decimal("99.4"),
        volume=Decimal("160"),
        close_time=candles[-1].close_time,
    )
    assert strategy.generate("BTCUSDT", weak_follow_through, candles, candles, metrics()) is None


def test_momentum_continuation_blocks_overextended_breakout_and_weak_flow(monkeypatch) -> None:
    candles = flat_candles(240)
    candles[-1] = Candle(
        open_time=candles[-1].open_time,
        open=Decimal("101.0"),
        high=Decimal("101.7"),
        low=Decimal("100.7"),
        close=Decimal("101.5"),
        volume=Decimal("220"),
        close_time=candles[-1].close_time,
    )
    monkeypatch.setattr(candidates, "atr", lambda items, period: [1.0] * len(items))
    monkeypatch.setattr(candidates, "ema", lambda values, period: [100.0 if period == 50 else 90.0] * len(values))
    monkeypatch.setattr(candidates, "_volume_ratio", lambda *_args, **_kwargs: Decimal("2.0"))

    strategy = MomentumContinuationStrategy(strategy_config(), FakeRegimeDetector(), FakeEdgeAnalyzer(score=Decimal("0.72")))
    signal = strategy.generate("BTCUSDT", candles, candles, candles, metrics())

    assert signal is not None
    assert signal.metadata["strategy"] == "MOMENTUM_CONTINUATION"
    assert signal.metadata["hour_utc"] == "4"

    monkeypatch.setattr(candidates, "atr", lambda items, period: [3.0] * len(items))
    assert strategy.generate("BTCUSDT", candles, candles, candles, metrics()) is None
    monkeypatch.setattr(candidates, "atr", lambda items, period: [1.0] * len(items))

    overextended = flat_candles(240)
    overextended[-1] = Candle(
        open_time=overextended[-1].open_time,
        open=Decimal("101.0"),
        high=Decimal("103.0"),
        low=Decimal("100.8"),
        close=Decimal("102.2"),
        volume=Decimal("220"),
        close_time=overextended[-1].close_time,
    )
    assert strategy.generate("BTCUSDT", overextended, overextended, overextended, metrics()) is None
    assert strategy.generate(
        "BTCUSDT",
        candles,
        candles,
        candles,
        metrics(order_book_imbalance=Decimal("0.00")),
    ) is None


def test_range_grid_requires_deeper_range_edge_and_better_rr(monkeypatch) -> None:
    def range_candles(last_close: str) -> list[Candle]:
        result = [
            Candle(
                open_time=i * 60_000,
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("100"),
                volume=Decimal("100"),
                close_time=(i + 1) * 60_000,
            )
            for i in range(80)
        ]
        result[-1] = Candle(
            open_time=result[-1].open_time,
            open=Decimal("95"),
            high=Decimal("96"),
            low=Decimal("90"),
            close=Decimal(last_close),
            volume=Decimal("120"),
            close_time=result[-1].close_time,
        )
        return result

    monkeypatch.setattr(candidates, "atr", lambda items, period: [4.0] * len(items))
    monkeypatch.setattr(candidates, "rsi", lambda values, period: [Decimal("34")] * len(values))

    strategy = RangeGridStrategy(strategy_config(), FakeRangeRegimeDetector())

    assert strategy.generate("ADAUSDT", range_candles("93.6"), range_candles("93.6"), range_candles("93.6"), metrics()) is None

    signal = strategy.generate("ADAUSDT", range_candles("92.0"), range_candles("92.0"), range_candles("92.0"), metrics())

    assert signal is not None
    assert signal.metadata["strategy"] == "RANGE_GRID"
    assert signal.metadata["hour_utc"] == "1"
    assert signal.metadata["entry_zone"] == "0.10"
    assert signal.metadata["rr"] == "1.40"
    assert signal.metadata["flow_safe"] == "True"
