from decimal import Decimal
from pathlib import Path

from trading_bot.config import load_config
from trading_bot.models import Candle, Direction, SymbolFilters
from trading_bot.strategy_engine.trend_breakout_4h import FOUR_HOURS_MS, TrendBreakout4hStrategy
from trading_bot.trade_manager.exit_plan import ExitPlanBuilder

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_700_000_000_000 - (1_700_000_000_000 % FOUR_HOURS_MS)


def _candle(i: int, close: float, spread: float = 1.0) -> Candle:
    c = Decimal(str(round(close, 4)))
    s = Decimal(str(spread))
    return Candle(
        open_time=T0 + i * FOUR_HOURS_MS,
        open=c,
        high=c + s,
        low=c - s,
        close=c,
        volume=Decimal("1000"),
        close_time=T0 + (i + 1) * FOUR_HOURS_MS - 1,
    )


def _series(direction: int = 1, breakout: float = 1.5) -> list[Candle]:
    """250-bar trend, 30-bar flat base, then one breakout bar."""
    candles = []
    price = 100.0 if direction > 0 else 300.0
    for i in range(250):
        price += 0.4 * direction
        candles.append(_candle(i, price))
    base = price
    for i in range(250, 280):
        candles.append(_candle(i, base + (0.3 if i % 2 else -0.3)))
    channel_edge = base + 0.3 + 1.0 if direction > 0 else base - 0.3 - 1.0
    candles.append(_candle(280, channel_edge + breakout * direction))
    return candles


def _fresh_15m(candles_4h):
    return [Candle(
        open_time=candles_4h[-1].close_time + 1,
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"),
        volume=Decimal("1"), close_time=candles_4h[-1].close_time + 15 * 60_000,
    )]


def test_long_breakout_in_uptrend():
    candles = _series(+1)
    signal = TrendBreakout4hStrategy().generate("SOLUSDT", _fresh_15m(candles), [], candles)
    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.stop_loss < signal.entry_price < signal.take_profit
    rr = (signal.take_profit - signal.entry_price) / (signal.entry_price - signal.stop_loss)
    assert rr == Decimal("8.0")
    assert signal.metadata["strategy"] == "TREND_BREAKOUT_4H"
    assert Decimal(signal.metadata["trailing_callback_rate_pct"]) > 0


def test_short_breakout_in_downtrend():
    candles = _series(-1)
    signal = TrendBreakout4hStrategy().generate("SOLUSDT", _fresh_15m(candles), [], candles)
    assert signal is not None and signal.direction == Direction.SHORT


def test_stale_breakout_is_ignored():
    candles = _series(+1)
    late = [Candle(
        open_time=candles[-1].close_time + 1, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
        close=Decimal("1"), volume=Decimal("1"), close_time=candles[-1].close_time + 3 * 60 * 60_000,
    )]
    assert TrendBreakout4hStrategy().generate("SOLUSDT", late, [], candles) is None


def test_no_signal_against_long_term_trend():
    # downtrend history, but an upside channel break -> filtered by EMA trend
    candles = _series(-1)
    last = candles[-1]
    up = float(max(c.high for c in candles[-21:-1])) + 1.5
    candles[-1] = _candle(280, up)
    assert TrendBreakout4hStrategy().generate("SOLUSDT", _fresh_15m(candles), [], candles) is None


def test_overextended_breakout_is_not_chased():
    candles = _series(+1, breakout=6.0)
    assert TrendBreakout4hStrategy().generate("SOLUSDT", _fresh_15m(candles), [], candles) is None


def test_second_breakout_bar_is_not_a_new_entry():
    candles = _series(+1)
    candles.append(_candle(281, float(candles[-1].close) + 1.0))
    assert TrendBreakout4hStrategy().generate("SOLUSDT", _fresh_15m(candles), [], candles) is None


def test_exit_plan_uses_strategy_trailing_and_profile():
    candles = _series(+1)
    signal = TrendBreakout4hStrategy().generate("SOLUSDT", _fresh_15m(candles), [], candles)
    cfg = load_config(ROOT / "config.yaml")
    builder = ExitPlanBuilder(cfg.trade_management)
    filters = SymbolFilters(
        symbol="SOLUSDT",
        tick_size=Decimal("0.0001"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )
    protection = builder.build_protection(signal, signal.style, filters)
    assert protection.trailing_callback_rate_pct == Decimal(signal.metadata["trailing_callback_rate_pct"])
    targets = builder.build_targets(signal, Decimal("10"), filters)
    assert [t.name for t in targets] == ["TP1", "RUNNER"]
    assert targets[0].reward_risk == Decimal("2.0") and targets[0].move_stop_to_breakeven
    assert targets[1].reward_risk == Decimal("8.0")
    assert cfg.strategy.mode_for_strategy("TREND_BREAKOUT_4H") == "shadow"
