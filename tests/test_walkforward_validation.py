from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import walkforward
from trading_bot.config import load_config
from trading_bot.models import Candle, Direction, Signal, TradingStyle


def candle(index: int, open_: str = "100", high: str = "100", low: str = "100", close: str = "100") -> Candle:
    return Candle(
        open_time=index * 60_000,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000"),
        close_time=(index + 1) * 60_000 - 1,
        quote_volume=Decimal("100000"),
    )


class OneShotStrategy:
    def __init__(self, signal: Signal) -> None:
        self.signal = signal
        self.calls = 0

    def generate(self, *_args, **_kwargs) -> Signal | None:
        self.calls += 1
        return self.signal if self.calls == 1 else None


class NoSignalStrategy:
    def generate(self, *_args, **_kwargs) -> None:
        return None


class RecordingNoSignalStrategy:
    def __init__(self) -> None:
        self.metrics = []

    def generate(self, _symbol, _c15m, _c1h, _c4h, metrics):
        self.metrics.append(metrics)
        return None


def test_walkforward_uses_production_signal_stop_without_1h_mr_rewrite() -> None:
    cfg = load_config(Path("config.yaml"), Path(".env.example"))
    signal = Signal(
        symbol="TESTUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("99"),
        take_profit=Decimal("103"),
        confidence=Decimal("0.8"),
        reason="test MR signal",
        metadata={"strategy": "MEAN_REVERSION"},
    )

    candles_1h = [candle(i, high="110", low="90") for i in range(260)]
    candles_4h = [candle(i) for i in range(100)]
    candles_15m = [candle(i) for i in range(1100)]
    candles_15m[1001] = candle(1001, high="100.5", low="98.5", close="99")

    rows = walkforward.test_window(
        symbol="TESTUSDT",
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        test_start_1h=0,
        test_end_1h=252,
        mr_strategy=OneShotStrategy(signal),  # type: ignore[arg-type]
        sqz_strategy=NoSignalStrategy(),  # type: ignore[arg-type]
        equity=Decimal("250"),
        window_idx=0,
        train_start=0,
        cfg=cfg,
    )

    mr_row = next(row for row in rows if row.strategy == "MEAN_REVERSION")
    assert mr_row.trades == 1
    assert mr_row.losses == 1
    assert mr_row.pnl < 0


def test_walkforward_builds_runtime_exit_profile_targets() -> None:
    cfg = load_config(Path("config.yaml"), Path(".env.example"))
    signal = Signal(
        symbol="BTCUSDT",
        direction=Direction.LONG,
        style=TradingStyle.INTRADAY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("112"),
        confidence=Decimal("0.8"),
        reason="test SQZ signal",
        metadata={"strategy": "SQUEEZE_BREAKOUT"},
    )

    targets = walkforward._partial_targets_for_signal("BTCUSDT", signal, Decimal("10"), cfg)

    assert [target["name"] for target in targets] == ["TP1", "TP2", "RUNNER"]
    assert [target["price"] for target in targets] == [Decimal("105.00000000"), Decimal("108.00000000"), Decimal("111.00000000")]


def test_walkforward_loads_market_metrics_from_csv(tmp_path: Path) -> None:
    path = tmp_path / "TESTUSDT_1h.csv"
    path.write_text(
        "\n".join(
            [
                "open_time,open,high,low,close,volume,quote_volume,spread_bps,top_book_liquidity_usdt,funding_rate,open_interest,open_interest_change_pct,aggressive_buy_sell_delta,order_book_imbalance",
                "60000,100,101,99,100.5,10,1000,4.5,2500000,-0.0002,123456,0.35,-0.18,0.12",
            ]
        ),
        encoding="utf-8",
    )

    metrics = walkforward.load_metric_overrides(path, "TESTUSDT")

    loaded = metrics[60000]
    assert loaded.symbol == "TESTUSDT"
    assert loaded.spread_bps == Decimal("4.5")
    assert loaded.top_book_liquidity_usdt == Decimal("2500000")
    assert loaded.funding_rate == Decimal("-0.0002")
    assert loaded.open_interest == Decimal("123456")
    assert loaded.open_interest_change_pct == Decimal("0.35")
    assert loaded.aggressive_buy_sell_delta == Decimal("-0.18")
    assert loaded.order_book_imbalance == Decimal("0.12")


def test_walkforward_passes_metric_overrides_into_strategy() -> None:
    cfg = load_config(Path("config.yaml"), Path(".env.example"))
    candles_1h = [candle(i) for i in range(260)]
    candles_4h = [candle(i) for i in range(100)]
    candles_15m = [candle(i) for i in range(1100)]
    recorder = RecordingNoSignalStrategy()
    custom_metrics = walkforward.MarketMetrics(
        symbol="TESTUSDT",
        quote_volume_24h=Decimal("1000000"),
        spread_bps=Decimal("2.5"),
        top_book_liquidity_usdt=Decimal("3000000"),
        funding_rate=Decimal("0.0003"),
        open_interest=Decimal("9000000"),
        open_interest_change_pct=Decimal("-0.25"),
        aggressive_buy_sell_delta=Decimal("0.22"),
    )

    walkforward.test_window(
        symbol="TESTUSDT",
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        test_start_1h=0,
        test_end_1h=260,
        mr_strategy=recorder,  # type: ignore[arg-type]
        sqz_strategy=NoSignalStrategy(),  # type: ignore[arg-type]
        equity=Decimal("250"),
        window_idx=0,
        train_start=0,
        cfg=cfg,
        metrics_by_open_time={candles_1h[250].open_time: custom_metrics},
    )

    assert recorder.metrics[0] is custom_metrics
