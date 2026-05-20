from __future__ import annotations

from decimal import Decimal

from trading_bot.config import StrategyConfig
from trading_bot.models import (
    Candle,
    Direction,
    EdgeSnapshot,
    MarketMetrics,
    MarketRegime,
    RegimeSnapshot,
)
import trading_bot.strategy_engine.mean_reversion as mean_reversion
from trading_bot.strategy_engine.mean_reversion import MeanReversionStrategy


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
        "enabled_strategies": ["MEAN_REVERSION"],
        "strategy_modes": {"MEAN_REVERSION": "shadow"},
        "mean_reversion_deviation_atr": Decimal("2.0"),
        "mean_reversion_rsi_oversold": Decimal("25"),
        "mean_reversion_rsi_overbought": Decimal("75"),
        "mean_reversion_take_profit_rr": Decimal("1.1"),
        "mean_reversion_min_volume_ratio": Decimal("1.05"),
        "mean_reversion_min_edge_score": Decimal("0.40"),
        "mean_reversion_min_confluence": 4,
        "mean_reversion_require_divergence": True,
        "mean_reversion_require_edge_confirmation": True,
    }
    values.update(overrides)
    return StrategyConfig(**values)


def candles(count: int, *, last_short_reversal: bool = False) -> list[Candle]:
    result: list[Candle] = []
    for i in range(count):
        volume = Decimal("125") if i >= count - 5 else Decimal("100")
        candle = Candle(
            open_time=i * 60_000,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=volume,
            close_time=(i + 1) * 60_000,
        )
        result.append(candle)
    if last_short_reversal:
        result[-1] = Candle(
            open_time=result[-1].open_time,
            open=Decimal("100"),
            high=Decimal("103"),
            low=Decimal("98"),
            close=Decimal("99"),
            volume=Decimal("125"),
            close_time=result[-1].close_time,
        )
    return result


def divergence_candles(count: int, *, previous_window_index: int, direction: Direction) -> list[Candle]:
    result = candles(count)
    window_start = count - 20
    previous_index = window_start + previous_window_index
    if direction == Direction.SHORT:
        result[previous_index] = Candle(
            open_time=result[previous_index].open_time,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("98"),
            close=Decimal("102"),
            volume=Decimal("100"),
            close_time=result[previous_index].close_time,
        )
        result[-1] = Candle(
            open_time=result[-1].open_time,
            open=Decimal("102"),
            high=Decimal("106"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("100"),
            close_time=result[-1].close_time,
        )
    else:
        result[previous_index] = Candle(
            open_time=result[previous_index].open_time,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("95"),
            close=Decimal("98"),
            volume=Decimal("100"),
            close_time=result[previous_index].close_time,
        )
        result[-1] = Candle(
            open_time=result[-1].open_time,
            open=Decimal("98"),
            high=Decimal("101"),
            low=Decimal("94"),
            close=Decimal("99"),
            volume=Decimal("100"),
            close_time=result[-1].close_time,
        )
    return result


class FakeRegimeDetector:
    def detect(self, _candles):
        return RegimeSnapshot(
            regime=MarketRegime.RANGE,
            atr_pct=Decimal("2"),
            trend_strength=Decimal("0.4"),
            momentum_pct=Decimal("0.1"),
            reason="test range",
        )


class FakeEdgeAnalyzer:
    def __init__(self, edge: EdgeSnapshot) -> None:
        self.edge = edge

    def analyze(self, *_args, **_kwargs) -> EdgeSnapshot:
        return self.edge


def metrics() -> MarketMetrics:
    return MarketMetrics(
        symbol="BTCUSDT",
        quote_volume_24h=Decimal("100000000"),
        spread_bps=Decimal("4"),
        aggressive_buy_sell_delta=Decimal("-0.12"),
    )


def edge_snapshot(*, confirms: bool = True) -> EdgeSnapshot:
    return EdgeSnapshot(
        liquidity_sweep=confirms,
        sweep_direction=Direction.SHORT if confirms else Direction.NONE,
        absorption=confirms,
        absorption_direction=Direction.SHORT if confirms else Direction.NONE,
        structure_break=False,
        structure_direction=Direction.NONE,
        liquidation_zone_nearby=False,
        score=Decimal("0.44") if confirms else Decimal("0.18"),
        reasons=("liquidity_sweep", "absorption") if confirms else (),
    )


def patch_indicators(monkeypatch, candles_15m, candles_4h, *, divergence: bool = True) -> None:
    monkeypatch.setattr(mean_reversion, "ema", lambda values, period: [90.0] * len(values))

    def fake_atr(items, _period):
        value = 4.0 if items is candles_4h else 1.0
        return [value] * len(items)

    monkeypatch.setattr(mean_reversion, "atr", fake_atr)
    monkeypatch.setattr(mean_reversion, "rsi", lambda values, period: [80.0] * len(values))
    monkeypatch.setattr(mean_reversion, "_rsi_divergence", lambda *_args, **_kwargs: divergence)


def test_mean_reversion_shadow_candidate_records_strict_evidence(monkeypatch) -> None:
    candles_15m = candles(220, last_short_reversal=True)
    candles_1h = candles(220)
    candles_4h = candles(220)
    patch_indicators(monkeypatch, candles_15m, candles_4h, divergence=True)

    strategy = MeanReversionStrategy(
        strategy_config(),
        FakeRegimeDetector(),
        FakeEdgeAnalyzer(edge_snapshot(confirms=True)),
    )

    signal = strategy.generate("BTCUSDT", candles_15m, candles_1h, candles_4h, metrics())

    assert signal is not None
    assert signal.direction == Direction.SHORT
    assert signal.metadata["strategy"] == "MEAN_REVERSION"
    assert signal.metadata["divergence"] == "yes"
    assert signal.metadata["edge_confirms"] == "True"
    assert signal.metadata["reversal_candle"] == "True"
    assert int(signal.metadata["mr_confluence"]) >= 6
    assert "edge" in signal.metadata["mr_confirmation_flags"]


def test_mean_reversion_blocks_shadow_candidate_without_required_divergence(monkeypatch) -> None:
    candles_15m = candles(220, last_short_reversal=True)
    candles_1h = candles(220)
    candles_4h = candles(220)
    patch_indicators(monkeypatch, candles_15m, candles_4h, divergence=False)

    strategy = MeanReversionStrategy(
        strategy_config(),
        FakeRegimeDetector(),
        FakeEdgeAnalyzer(edge_snapshot(confirms=True)),
    )

    assert strategy.generate("BTCUSDT", candles_15m, candles_1h, candles_4h, metrics()) is None


def test_rsi_divergence_uses_absolute_price_extreme_index_for_short(monkeypatch) -> None:
    items = divergence_candles(45, previous_window_index=7, direction=Direction.SHORT)
    rsi_values = [50.0] * len(items)
    rsi_values[len(items) - 20 + 7] = 80.0
    rsi_values[-1] = 70.0
    monkeypatch.setattr(mean_reversion, "rsi", lambda *_args, **_kwargs: rsi_values)

    assert mean_reversion._rsi_divergence(items, Direction.SHORT, rsi_period=14, lookback=20)


def test_rsi_divergence_uses_absolute_price_extreme_index_for_long(monkeypatch) -> None:
    items = divergence_candles(45, previous_window_index=8, direction=Direction.LONG)
    rsi_values = [50.0] * len(items)
    rsi_values[len(items) - 20 + 8] = 20.0
    rsi_values[-1] = 25.0
    monkeypatch.setattr(mean_reversion, "rsi", lambda *_args, **_kwargs: rsi_values)

    assert mean_reversion._rsi_divergence(items, Direction.LONG, rsi_period=14, lookback=20)


def test_mean_reversion_blocks_shadow_candidate_without_edge_or_reversal(monkeypatch) -> None:
    candles_15m = candles(220, last_short_reversal=False)
    candles_1h = candles(220)
    candles_4h = candles(220)
    patch_indicators(monkeypatch, candles_15m, candles_4h, divergence=True)

    strategy = MeanReversionStrategy(
        strategy_config(),
        FakeRegimeDetector(),
        FakeEdgeAnalyzer(edge_snapshot(confirms=False)),
    )

    assert strategy.generate("BTCUSDT", candles_15m, candles_1h, candles_4h, metrics()) is None
