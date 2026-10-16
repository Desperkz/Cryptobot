"""Closed-bar VWAP pullback continuation hypothesis, separate from reversion V2.

No RSI stretch and no 24h efficiency gate. A closed 1h EMA20/50 trend sets the
direction; a 15m VWAP retest and subsequent prior-bar break confirm continuation.
The declared rules are fixed before the first replay, with no parameter sweep.
"""
import math

from trading_bot.strategy_engine.diversified import ResearchPlan, volume_ratio, vwap
from trading_bot.strategy_engine.indicators import atr, closes, ema

VERSION = 'VWAP_TREND_PULLBACK_V1'
RULE = {
    'trend_timeframe': '1h', 'fast_ema': 20, 'slow_ema': 50, 'slope_bars': 3,
    'vwap_bars_15m': 96, 'touch_tolerance_atr': .15,
    'max_setup_penetration_atr': .35, 'min_reclaim_atr': .1,
    'max_entry_extension_atr': .8, 'min_confirmation_volume_ratio': 1.,
    'minimum_stop_atr': 1.2, 'maximum_stop_atr': 2.5,
    'structural_stop_buffer_atr': .15, 'target_r': 2., 'horizon_hours': 8,
    'minimum_atr_pct': .0015,
}


def evaluate(symbol, c15, c1h, c4h):
    diagnostic = {'strategy': VERSION, 'symbol': symbol, 'reason': 'insufficient_candles'}
    def reject(reason):
        diagnostic['reason'] = reason
        return None, diagnostic
    if min(len(c15),len(c1h),len(c4h))<205:
        return reject('insufficient_candles')
    stamp = c15[-1].close_time
    for candles,interval in ((c15,900000),(c1h,3600000),(c4h,14400000)):
        if candles[-1].close_time>stamp:
            return reject('future_higher_timeframe')
        if stamp-candles[-1].close_time>=interval:
            return reject('stale_higher_timeframe')
        if any(b.open_time-a.open_time!=interval for a,b in zip(candles,candles[1:])):
            return reject('candle_gap')
        for candle in candles:
            values=[float(v) for v in (candle.open,candle.high,candle.low,candle.close,candle.volume)]
            if (not all(math.isfinite(v) for v in values) or min(values[:4])<=0 or values[4]<0
                    or not values[2]<=min(values[0],values[3])<=max(values[0],values[3])<=values[1]
                    or candle.close_time!=candle.open_time+interval-1 or candle.open_time%interval):
                return reject('invalid_candle')
    last,setup = c15[-1],c15[-2]
    entry = float(last.close)
    a = atr(c15,14)[-1]
    if not math.isfinite(a) or a<=0 or a/entry<RULE['minimum_atr_pct']:
        return reject('volatility_below_cost_floor')
    fast,slow = ema(closes(c1h),20),ema(closes(c1h),50)
    slope = fast[-1]-fast[-4]
    price1h = float(c1h[-1].close)
    sign = 1 if price1h>fast[-1]>slow[-1] and slope>0 else -1 if price1h<fast[-1]<slow[-1] and slope<0 else 0
    diagnostic.update(atr=a,fast_ema=fast[-1],slow_ema=slow[-1],slope_3h=slope,
        direction='LONG' if sign==1 else 'SHORT' if sign==-1 else None,bar_close_ms=stamp)
    if not sign:
        return reject('no_closed_hourly_trend')
    previous_anchor,anchor = vwap(c15[-97:-1]),vwap(c15[-96:])
    if previous_anchor is None or anchor is None:
        return reject('zero_volume')
    diagnostic.update(vwap=anchor,setup_vwap=previous_anchor,volume_ratio=volume_ratio(c15))
    # Setup must actually intersect the VWAP area, not merely be far below it.
    touched = (float(setup.low)<=previous_anchor+.15*a and float(setup.high)>=previous_anchor-.15*a)
    if not touched:
        return reject('no_vwap_retest')
    if sign*(float(setup.close)-previous_anchor)<-.35*a:
        return reject('setup_closed_too_far_through_vwap')
    extension = sign*(entry-anchor)/a
    diagnostic['extension_atr'] = extension
    if not .1<=extension<=.8:
        return reject('reclaim_extension')
    trigger = float(setup.high) if sign==1 else float(setup.low)
    if sign*(entry-trigger)<=0 or sign*(entry-float(last.open))<=0:
        return reject('no_closed_bar_confirmation')
    if diagnostic['volume_ratio']<1.:
        return reject('weak_confirmation_volume')
    extreme = min(float(c.low) for c in c15[-3:]) if sign==1 else max(float(c.high) for c in c15[-3:])
    distance = max(1.2*a,sign*(entry-extreme)+.15*a)
    if distance>2.5*a:
        return reject('structural_stop_too_far')
    stop,target = entry-sign*distance,entry+sign*2*distance
    if min(stop,target)<=0:
        return reject('invalid_target_or_stop')
    diagnostic.update(reason='SIGNAL',stop_atr=distance/a,reward_risk=2.)
    return ResearchPlan(VERSION,symbol,'LONG' if sign==1 else 'SHORT',stamp,entry,stop,target,8),diagnostic
