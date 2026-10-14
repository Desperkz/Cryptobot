"""TREND_BREAKOUT_4H - time-series momentum on the 4h frame.

Why this exists
---------------
Every other strategy in the bot works on 15m/1h with stops of ~1-2 ATR(1h).
On liquid perps that is roughly 0.5-2% of price, so a round trip of
taker fees + slippage (~18-20 bps) costs 0.1-0.3R *before* any edge, and
fixed 2.2R targets cut off the right tail that breakout systems live on.

Time-series momentum (Donchian/channel breakout in the direction of the
long-term trend, wide ATR stop, trailing exit, no hard profit cap) is the
most persistently documented directional effect in crypto and in futures in
general.  On the 4h frame the stop is ~2-6% of price, so the same costs are
only ~0.03-0.08R, and the trailing exit lets the rare large trend pay for the
many small losers.  Expect a low win rate (30-40%) and a few large winners.

Rules (all on CLOSED 4h candles)
--------------------------------
* trend filter: EMA(50) above EMA(200) and close above EMA(200) for LONG
  (mirror for SHORT);
* trigger: close breaks the highest high / lowest low of the previous
  ``channel`` bars, and the previous close had NOT already broken its own
  channel (first breakout bar only - no pyramiding into an old move);
* no chasing: the close is at most ``max_extension_atr`` ATR beyond the
  channel;
* freshness: only within ``max_signal_age_minutes`` after the 4h close, so the
  per-minute polling loop cannot enter a breakout hours late;
* stop: ``stop_atr`` x ATR(4h); target: a far ``take_profit_rr`` that acts as
  a cap of last resort; the real exit is the ATR trailing stop carried in
  ``trailing_callback_rate_pct``.

This file has no tuned-to-PnL parameters; defaults are textbook values
(20-bar channel, 50/200 EMA, 2 ATR stop, ~3 ATR chandelier trail).  It
starts as a SHADOW strategy and must earn its way to paper.
"""
from __future__ import annotations

from decimal import Decimal

from trading_bot.models import Candle, Direction, MarketMetrics, Signal, TradingStyle, to_decimal
from trading_bot.strategy_engine.indicators import atr, closes, ema

STRATEGY_NAME = "TREND_BREAKOUT_4H"
FOUR_HOURS_MS = 4 * 60 * 60 * 1000


class TrendBreakout4hStrategy:
    def __init__(
        self,
        *,
        channel: int = 20,
        ema_fast: int = 50,
        ema_slow: int = 200,
        atr_period: int = 14,
        stop_atr: Decimal = Decimal("2.0"),
        trail_atr: Decimal = Decimal("3.0"),
        take_profit_rr: Decimal = Decimal("8.0"),
        max_extension_atr: Decimal = Decimal("1.0"),
        min_atr_pct: Decimal = Decimal("0.6"),
        max_atr_pct: Decimal = Decimal("8.0"),
        max_signal_age_minutes: int = 60,
    ) -> None:
        self.channel = channel
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.stop_atr = stop_atr
        self.trail_atr = trail_atr
        self.take_profit_rr = take_profit_rr
        self.max_extension_atr = max_extension_atr
        self.min_atr_pct = min_atr_pct
        self.max_atr_pct = max_atr_pct
        self.max_signal_age_minutes = max_signal_age_minutes

    @property
    def min_bars(self) -> int:
        return max(self.ema_slow + 10, self.channel + 3)

    def generate(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        metrics: MarketMetrics | None = None,
        *,
        now_ms: int | None = None,
    ) -> Signal | None:
        if len(candles_4h) < self.min_bars:
            return None
        last = candles_4h[-1]

        # Freshness: the most recent closed 15m candle tells us "now" without a
        # wall clock, which keeps backtests and live identical.
        reference_ms = now_ms
        if reference_ms is None and candles_15m:
            reference_ms = candles_15m[-1].close_time
        if reference_ms is not None:
            age_minutes = (reference_ms - last.close_time) / 60_000
            if age_minutes > self.max_signal_age_minutes:
                return None

        close_values = closes(candles_4h)
        ema_fast = ema(close_values, self.ema_fast)
        ema_slow = ema(close_values, self.ema_slow)
        atr_values = atr(candles_4h, self.atr_period)
        atr_now = to_decimal(atr_values[-1])
        close = last.close
        if atr_now <= 0 or close <= 0:
            return None
        atr_pct = atr_now / close * Decimal("100")
        if atr_pct < self.min_atr_pct or atr_pct > self.max_atr_pct:
            return None

        window = candles_4h[-(self.channel + 1) : -1]
        prev_window = candles_4h[-(self.channel + 2) : -2]
        upper = max(c.high for c in window)
        lower = min(c.low for c in window)
        prev_upper = max(c.high for c in prev_window)
        prev_lower = min(c.low for c in prev_window)
        prev_close = candles_4h[-2].close

        fast = to_decimal(ema_fast[-1])
        slow = to_decimal(ema_slow[-1])
        direction: Direction | None = None
        breakout_distance = Decimal("0")
        if fast > slow and close > slow and close > upper and prev_close <= prev_upper:
            direction = Direction.LONG
            breakout_distance = close - upper
        elif fast < slow and close < slow and close < lower and prev_close >= prev_lower:
            direction = Direction.SHORT
            breakout_distance = lower - close
        if direction is None:
            return None

        extension_atr = breakout_distance / atr_now
        if extension_atr > self.max_extension_atr:
            return None

        stop_distance = atr_now * self.stop_atr
        if direction == Direction.LONG:
            stop_loss = close - stop_distance
            take_profit = close + stop_distance * self.take_profit_rr
        else:
            stop_loss = close + stop_distance
            take_profit = close - stop_distance * self.take_profit_rr
        if stop_loss <= 0 or take_profit <= 0:
            return None

        trail_pct = (atr_pct * self.trail_atr).quantize(Decimal("0.01"))
        trail_pct = min(max(trail_pct, Decimal("0.5")), Decimal("10"))
        trend_gap_atr = abs(fast - slow) / atr_now
        confidence = Decimal("0.60")
        if trend_gap_atr >= Decimal("2"):
            confidence += Decimal("0.05")
        if extension_atr <= Decimal("0.5"):
            confidence += Decimal("0.05")

        return Signal(
            symbol=symbol,
            direction=direction,
            style=TradingStyle.SWING,
            entry_price=close,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            reason=(
                f"{STRATEGY_NAME}: {self.channel}-bar 4h channel break, "
                f"ext={extension_atr:.2f}ATR, ema{self.ema_fast}/{self.ema_slow} trend, "
                f"stop={self.stop_atr}ATR, trail={trail_pct}%"
            ),
            timeframe="4h",
            metadata={
                "strategy": STRATEGY_NAME,
                "strategy_logic_version": "trend_breakout_4h_v1",
                "channel_bars": self.channel,
                "channel_high": str(upper),
                "channel_low": str(lower),
                "breakout_extension_atr": str(extension_atr.quantize(Decimal("0.0001"))),
                "ema_fast": str(fast),
                "ema_slow": str(slow),
                "trend_gap_atr": str(trend_gap_atr.quantize(Decimal("0.01"))),
                "atr_4h": str(atr_now),
                "atr_pct": str(atr_pct.quantize(Decimal("0.0001"))),
                "stop_atr_multiplier": str(self.stop_atr),
                "rr": str(self.take_profit_rr),
                "trailing_callback_rate_pct": str(trail_pct),
                "signal_bar_close_time": last.close_time,
            },
        )
