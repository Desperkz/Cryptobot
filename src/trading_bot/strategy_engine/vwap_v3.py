"""One prospective VWAP admission hypothesis; never changes a V2 signal/exit.

The 24h efficiency guard can miss a shorter directional move. Refuse fading
the 1h EMA20 when both its three-hour slope and price location oppose entry.
Periods are a declared hypothesis, not a parameter search or a profitability claim.
"""
import math
from trading_bot.strategy_engine.indicators import closes, ema

VERSION = 'VWAP_REVERSION_V3_LOCAL_TREND'
RULE = {'ema_period': 20, 'slope_bars': 3,
    'reject': 'price and EMA20 three-hour slope both oppose the trade',
    'entry_exit_risk': 'unchanged matched V2 observations; admission filter only'}


def admission(direction, c1h, signal_bar_ms):
    if direction not in ('LONG', 'SHORT') or len(c1h) < 205:
        raise ValueError('Invalid direction or insufficient closed hourly history')
    if any(c.close_time > signal_bar_ms for c in c1h):
        raise ValueError('Future hourly candle')
    if signal_bar_ms-c1h[-1].close_time>=3600000:
        raise ValueError('Stale hourly candle')
    if any(not math.isfinite(float(c.close)) or c.close<=0 for c in c1h):
        raise ValueError('Invalid hourly close')
    if any(b.open_time-a.open_time != 3600000 for a,b in zip(c1h,c1h[1:])):
        raise ValueError('Hourly candle gap')
    values = ema(closes(c1h), RULE['ema_period'])
    sign = 1 if direction == 'LONG' else -1
    location = sign*(float(c1h[-1].close)-values[-1])
    slope = sign*(values[-1]-values[-1-RULE['slope_bars']])
    blocked = location < 0 and slope < 0
    return {'allowed': not blocked, 'reason': 'LOCAL_TREND_AGAINST' if blocked else 'LOCAL_TREND_PASS',
        'version': VERSION, 'ema20': values[-1], 'signed_location': location,
        'signed_slope_3h': slope, 'hour_close_ms': c1h[-1].close_time}
