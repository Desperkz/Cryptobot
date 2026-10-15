"""Closed-bar research rules. No orders, credentials, config mutation or I/O.

V2 names deliberately separate new hypotheses from frozen historical controls.
All thresholds are explicit and can be overridden in a NEW research cohort.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from trading_bot.strategy_engine.indicators import atr, closes, ema, rsi

DEFAULTS = {
    'MEAN_REVERSION_V2': {'max_efficiency': .35, 'min_deviation_atr': 1.5, 'rsi_long': 35., 'rsi_short': 65., 'min_progress_atr': .1, 'stop_atr': 1.2, 'min_rr': 1.2, 'horizon_hours': 8},
    'VWAP_REVERSION_V2': {'max_efficiency': .45, 'min_deviation_atr': 1.6, 'max_deviation_atr': 6., 'rsi_long': 42., 'rsi_short': 58., 'min_progress_atr': .1, 'min_volume_ratio': .65, 'stop_atr': 1.2, 'min_rr': 1.2, 'horizon_hours': 8},
    'RANGE_REBOUND_V2': {'max_efficiency': .35, 'min_range_atr': 3., 'max_range_atr': 12., 'entry_zone': .2, 'min_touches': 2, 'stop_atr': 1., 'min_rr': 1.2, 'horizon_hours': 8},
    'SWEEP_RECLAIM_V2': {'max_efficiency': .65, 'sweep_atr': .05, 'reclaim_atr': .15, 'min_volume_ratio': 1.1, 'stop_atr': 1., 'reward_risk': 2., 'min_rr': 1.2, 'horizon_hours': 6},
    'MOMENTUM_CONTINUATION_V2': {'min_volume_ratio': 1.3, 'min_extension_atr': .05, 'max_extension_atr': .8, 'min_body_atr': .2, 'max_body_atr': 1.4, 'stop_atr': 1.5, 'reward_risk': 2.5, 'min_rr': 1.2, 'horizon_hours': 12},
    'SESSION_RANGE_BREAKOUT_V1': {'range_start_utc': 0, 'range_end_utc': 6, 'entry_end_utc': 12, 'min_range_atr': 2., 'max_range_atr': 12., 'min_volume_ratio': 1.2, 'min_extension_atr': .1, 'max_extension_atr': .8, 'stop_atr': 1.5, 'reward_risk': 2.5, 'min_rr': 1.2, 'horizon_hours': 12},
}


@dataclass(frozen=True)
class ResearchPlan:
    strategy: str
    symbol: str
    direction: str
    bar_close_ms: int
    entry: float
    stop: float
    target: float
    horizon_hours: int


def efficiency(candles, lookback=24):
    values = [float(c.close) for c in candles[-lookback-1:]]
    travel = sum(abs(b-a) for a, b in zip(values, values[1:]))
    return abs(values[-1]-values[0])/travel if travel else 0.


def volume_ratio(candles):
    prior = [float(c.volume) for c in candles[-21:-1]]
    average = sum(prior)/len(prior) if prior else 0
    return float(candles[-1].volume)/average if average else 0.


def vwap(candles):
    weight = sum(float(c.volume) for c in candles)
    return sum(float((c.high+c.low+c.close)/3)*float(c.volume) for c in candles)/weight if weight else None


def parameters(strategy, overrides=None):
    opts = dict(DEFAULTS[strategy])
    if set(overrides or {})-opts.keys():
        raise ValueError('Unknown strategy parameters')
    opts.update(overrides or {})
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0 for v in opts.values()):
        raise ValueError('Parameters must be finite non-negative numbers')
    if not 1 <= opts['horizon_hours'] <= 24 or int(opts['horizon_hours']) != opts['horizon_hours'] or opts['stop_atr'] <= 0 or opts['min_rr'] <= 0:
        raise ValueError('Invalid horizon, stop or target')
    if 'max_efficiency' in opts and not 0 < opts['max_efficiency'] <= 1:
        raise ValueError('Invalid efficiency threshold')
    if strategy == 'SESSION_RANGE_BREAKOUT_V1':
        if not 0 <= opts['range_start_utc'] < opts['range_end_utc'] < opts['entry_end_utc'] <= 24:
            raise ValueError('Invalid UTC session')
        if any(int(opts[k]) != opts[k] for k in ('range_start_utc','range_end_utc','entry_end_utc')):
            raise ValueError('Session boundaries must be whole UTC hours')
    return opts


def evaluate(strategy, symbol, c15, c1h, c4h, *, overrides=None):
    """Inputs MUST be already closed and time-aligned. Reject future HTF bars."""
    opts = parameters(strategy, overrides)
    diagnostic = {'strategy': strategy, 'symbol': symbol, 'reason': 'insufficient_candles'}
    def reject(reason):
        diagnostic['reason'] = reason
        return None, diagnostic
    if len(c15) < 205 or len(c1h) < 205 or len(c4h) < 205:
        return reject('insufficient_candles')
    now = c15[-1].close_time
    if c1h[-1].close_time > now or c4h[-1].close_time > now:
        return reject('future_higher_timeframe')
    last, previous = c15[-1], c15[-2]
    entry, prev = float(last.close), float(previous.close)
    a = atr(c15, 14)[-1]
    if not all(math.isfinite(x) and x > 0 for x in (a, entry, prev)):
        return reject('invalid_price_or_atr')
    er = efficiency(c1h)
    diagnostic.update(atr=a, efficiency=er, volume_ratio=volume_ratio(c15), bar_close_ms=now)
    if a/entry < .0015:
        return reject('volatility_below_cost_floor')
    if 'max_efficiency' in opts and er > opts['max_efficiency']:
        return reject('persistent_trend')
    side = 0
    stop, target = None, None
    if strategy in ('MEAN_REVERSION_V2', 'VWAP_REVERSION_V2'):
        if strategy == 'MEAN_REVERSION_V2':
            anchor_prev = ema(closes(c15[:-1]), 20)[-1]
            anchor = ema(closes(c15), 20)[-1]
            mean1h = ema(closes(c1h), 50)
            if abs(mean1h[-1]-mean1h[-7]) > atr(c1h,14)[-1]*.6:
                return reject('mean_drifting')
        else:
            anchor_prev, anchor = vwap(c15[-97:-1]), vwap(c15[-96:])
            if anchor_prev is None or anchor is None:
                return reject('zero_volume')
            if diagnostic['volume_ratio'] < opts['min_volume_ratio']:
                return reject('weak_volume')
        deviation = (prev-anchor_prev)/a
        progress = (abs(prev-anchor_prev)-abs(entry-anchor))/a
        oscillator = rsi(closes(c15[:-1]),14)[-1]
        diagnostic.update(deviation_atr=deviation, progress_atr=progress, rsi=oscillator, anchor=anchor)
        if abs(deviation) < opts['min_deviation_atr']:
            return reject('no_stretch')
        if abs(deviation) > opts.get('max_deviation_atr', 6.):
            return reject('extreme_stretch')
        if deviation < 0 and oscillator <= opts['rsi_long']:
            side = 1
        elif deviation > 0 and oscillator >= opts['rsi_short']:
            side = -1
        else:
            return reject('rsi_not_stretched')
        if side*(entry-prev) <= 0 or side*(entry-float(last.open)) <= .05*a or progress < opts['min_progress_atr']:
            return reject('no_turn_toward_mean')
        stop = entry-side*max(opts['stop_atr']*a, side*(entry-(min(float(c.low) for c in c15[-3:]) if side==1 else max(float(c.high) for c in c15[-3:])))+.15*a)
        target = anchor
    elif strategy == 'RANGE_REBOUND_V2':
        prior = c15[-50:-2]
        upper, lower = max(float(c.high) for c in prior), min(float(c.low) for c in prior)
        width = (upper-lower)/a
        diagnostic['range_atr'] = width
        if not opts['min_range_atr'] <= width <= opts['max_range_atr']:
            return reject('range_width')
        # Consecutive candles around the same touch count as ONE level test.
        def touches(values):
            return sum(value and (i == 0 or not values[i-1]) for i,value in enumerate(values))
        highs = [float(c.high) >= upper-.35*a for c in prior]
        lows = [float(c.low) <= lower+.35*a for c in prior]
        if min(touches(highs),touches(lows)) < opts['min_touches']:
            return reject('untested_boundaries')
        pos = (prev-lower)/(upper-lower)
        if not lower < entry < upper:
            return reject('range_broken')
        if pos <= opts['entry_zone'] and entry > prev and last.close > last.open:
            side = 1
        elif pos >= 1-opts['entry_zone'] and entry < prev and last.close < last.open:
            side = -1
        else:
            return reject('no_boundary_rebound')
        stop = min(lower-.2*a, entry-opts['stop_atr']*a) if side==1 else max(upper+.2*a,entry+opts['stop_atr']*a)
        target = (upper+lower)/2
    elif strategy == 'SWEEP_RECLAIM_V2':
        prior = c15[-20:-2]
        upper,lower = max(float(c.high) for c in prior),min(float(c.low) for c in prior)
        if float(previous.low) < lower-opts['sweep_atr']*a and entry > lower+opts['reclaim_atr']*a and entry > prev and last.close > last.open:
            side=1
        elif float(previous.high) > upper+opts['sweep_atr']*a and entry < upper-opts['reclaim_atr']*a and entry < prev and last.close < last.open:
            side=-1
        else:
            return reject('no_confirmed_two_bar_sweep')
        if volume_ratio(c15[:-1]) < opts['min_volume_ratio']:
            return reject('weak_sweep_volume')
        stop = min(float(previous.low),float(last.low))-.15*a if side==1 else max(float(previous.high),float(last.high))+.15*a
        stop = min(stop,entry-opts['stop_atr']*a) if side==1 else max(stop,entry+opts['stop_atr']*a)
        target = entry+side*abs(entry-stop)*opts['reward_risk']
    else:
        if strategy == 'MOMENTUM_CONTINUATION_V2':
            fast,slow=ema(closes(c4h),50)[-1],ema(closes(c4h),200)[-1]
            side=1 if entry>fast>slow else -1 if entry<fast<slow else 0
            if not side:return reject('no_directional_trend')
            prior=c15[-21:-1]
            upper,lower=max(float(c.high) for c in prior),min(float(c.low) for c in prior)
            previous_prior=c15[-22:-2]
            if side==1 and prev>max(float(c.high) for c in previous_prior):return reject('already_broken')
            if side==-1 and prev<min(float(c.low) for c in previous_prior):return reject('already_broken')
            body=abs(entry-float(last.open))/a
            if not opts['min_body_atr']<=body<=opts['max_body_atr'] or side*(entry-float(last.open))<=0:
                return reject('impulse_body')
        else:
            day=last.open_time//86_400_000*86_400_000
            hour=(last.open_time-day)/3_600_000
            if not opts['range_end_utc']<=hour<opts['entry_end_utc']:
                return reject('outside_entry_session')
            session=[c for c in c15 if day+opts['range_start_utc']*3_600_000<=c.open_time<day+opts['range_end_utc']*3_600_000]
            expected=int((opts['range_end_utc']-opts['range_start_utc'])*4)
            if len(session)!=expected or any(b.open_time-a_.open_time!=900000 for a_,b in zip(session,session[1:])):
                return reject('incomplete_session')
            upper,lower=max(float(c.high) for c in session),min(float(c.low) for c in session)
            if not opts['min_range_atr']<=(upper-lower)/a<=opts['max_range_atr']:
                return reject('session_width')
            side=1 if entry>upper and prev<=upper else -1 if entry<lower and prev>=lower else 0
            if not side:return reject('no_session_break')
            diagnostic.update(session_high=upper,session_low=lower,session_day_ms=day)
        extension=side*(entry-(upper if side==1 else lower))/a
        diagnostic['extension_atr']=extension
        if not opts['min_extension_atr']<=extension<=opts['max_extension_atr']:
            return reject('breakout_extension')
        if diagnostic['volume_ratio']<opts['min_volume_ratio']:
            return reject('weak_breakout_volume')
        stop=entry-side*opts['stop_atr']*a
        target=entry+side*abs(entry-stop)*opts['reward_risk']
    risk=side*(entry-stop)
    rr=side*(target-entry)/risk if risk>0 else 0
    diagnostic['reward_risk']=rr
    if not all(math.isfinite(x) and x>0 for x in (entry,stop,target)) or risk<=0 or rr<opts['min_rr']:
        return reject('insufficient_target_space')
    diagnostic['reason']='SIGNAL'
    return ResearchPlan(strategy,symbol,'LONG' if side==1 else 'SHORT',now,entry,stop,target,int(opts['horizon_hours'])),diagnostic
