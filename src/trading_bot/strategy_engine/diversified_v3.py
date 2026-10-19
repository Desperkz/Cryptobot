"""Six revised, independently named shadow hypotheses. Closed bars only.

Frozen before replay, 19 October 2026. No outcome-based direction/symbol bans,
parameter search, position scaling or changes to the V2 control rules.
"""
import math
from trading_bot.strategy_engine.diversified import ResearchPlan, efficiency, volume_ratio, vwap, evaluate as control
from trading_bot.strategy_engine.indicators import atr, closes, ema, rsi

RULES = {
    'MEAN_REVERSION_V3': {'change':'Break of setup extreme confirms reversal; local trend check', 'horizon_hours':8},
    'VWAP_REVERSION_V3_RECLAIM': {'change':'Confirmed reversal of stretch; oppose only persistent local trend', 'horizon_hours':8},
    'RANGE_REBOUND_V3': {'change':'Trimmed repeatedly tested boundaries and confirmed bounce', 'horizon_hours':8},
    'SWEEP_RECLAIM_V3': {'change':'Setup must close back inside range; avoid late entries', 'horizon_hours':6},
    'MOMENTUM_CONTINUATION_V3': {'change':'Original breakout, strong candle close; target 2R instead of 2.5R', 'horizon_hours':12},
    'SESSION_RANGE_BREAKOUT_V2': {'change':'Paused: revised session breakout failed historical comparison', 'horizon_hours':12, 'forward_enabled':False},
}
MAX_EXPENSE_FRACTION = .35


def validate_series(candles,interval):
    for i,c in enumerate(candles):
        o,h,l,p,v=map(float,(c.open,c.high,c.low,c.close,c.volume))
        if (not all(math.isfinite(x) for x in (o,h,l,p,v)) or not 0<l<=min(o,p)<=max(o,p)<=h or v<0
                or c.open_time%interval or c.close_time!=c.open_time+interval-1):return 'invalid_candle'
        if i and c.open_time-candles[i-1].open_time!=interval:return 'candle_gap'
    return None


def valid(c15,c1,c4,*,full=True):
    if min(len(c15),len(c1),len(c4))<205:return 'insufficient_candles'
    stamp=c15[-1].close_time
    for candles,interval in ((c15,900000),(c1,3600000),(c4,14400000)):
        if candles[-1].close_time>stamp:return 'future_higher_timeframe'
        if stamp-candles[-1].close_time>=interval:return 'stale_higher_timeframe'
        if full:
            problem=validate_series(candles,interval)
            if problem:return problem
    return None


def evaluate(name,symbol,c15,c1,c4,*,_validated=False):
    if name not in RULES:raise ValueError('Unknown revised rule')
    d={'strategy':name,'symbol':symbol,'reason':'insufficient_candles'}
    def reject(reason):d['reason']=reason;return None,d
    # Offline replay may validate complete series once. Production always uses
    # the default full validation. Length/time alignment checks remain enabled.
    problem=valid(c15,c1,c4,full=not _validated)
    if problem:return reject(problem)
    last,setup=c15[-1],c15[-2];entry=float(last.close)
    a=atr(c15,14)[-1]
    if not math.isfinite(a) or a<=0:return reject('invalid_price_or_atr')
    d.update(atr=a,bar_close_ms=last.close_time,volume_ratio=volume_ratio(c15))
    if a/entry<.0015:return reject('volatility_below_cost_floor')
    fast,slow=ema(closes(c1),20),ema(closes(c1),50)
    price1=float(c1[-1].close);er6=efficiency(c1,6);er24=efficiency(c1,24)
    trend=1 if price1>fast[-1]>slow[-1] and fast[-1]>fast[-4] else -1 if price1<fast[-1]<slow[-1] and fast[-1]<fast[-4] else 0
    d.update(local_efficiency=er6,hourly_trend=trend)
    sign=0;target=None;minimum_stop=1.2

    def confirmed(side):
        width=float(last.high-last.low)
        location=(entry-float(last.low))/width if width else .5
        return (side*(entry-(float(setup.high) if side==1 else float(setup.low)))>.05*a
                and side*(entry-float(last.open))>=.15*a and (location>=.65 if side==1 else location<=.35))

    if name in ('MEAN_REVERSION_V3','VWAP_REVERSION_V3_RECLAIM'):
        is_vwap=name.startswith('VWAP')
        anchor=vwap(c15[-96:]) if is_vwap else ema(closes(c15),20)[-1]
        before=vwap(c15[-97:-1]) if is_vwap else ema(closes(c15[:-1]),20)[-1]
        if anchor is None or before is None:return reject('zero_volume')
        deviation=(float(setup.close)-before)/a;oscillator=rsi(closes(c15[:-1]),14)[-1]
        d.update(anchor=anchor,deviation_atr=deviation,rsi=oscillator)
        if not (1.6 if is_vwap else 1.5)<=abs(deviation)<=6:return reject('no_stretch')
        sign=1 if deviation<0 and oscillator<=(42 if is_vwap else 35) else -1 if deviation>0 and oscillator>=(58 if is_vwap else 65) else 0
        if not sign:return reject('rsi_not_stretched')
        if er24>(.45 if is_vwap else .35):return reject('persistent_trend')
        # A 24h sideways reading can conceal a fresh directional 6h move.
        if er6>.60 and trend==-sign:return reject('opposing_local_trend')
        if not is_vwap and abs(slow[-1]-slow[-7])>atr(c1,14)[-1]*.6:return reject('mean_drifting')
        if is_vwap and d['volume_ratio']<.65:return reject('weak_volume')
        if not confirmed(sign):return reject('reversal_not_confirmed')
        target=anchor
    elif name=='RANGE_REBOUND_V3':
        prior=c15[-50:-2]
        upper=sorted(float(c.high) for c in prior)[-3];lower=sorted(float(c.low) for c in prior)[2]
        width=(upper-lower)/a;d.update(range_high=upper,range_low=lower,range_atr=width)
        if not 3<=width<=12:return reject('range_width')
        if er24>.35:return reject('persistent_trend')
        # Separate tests by >=3 bars; repeated candles at a level are one visit.
        def tests(high):
            last_i=-100;count=0
            for i,c in enumerate(prior):
                touch=float(c.high)>=upper-.25*a if high else float(c.low)<=lower+.25*a
                if touch and i-last_i>=3:count+=1;last_i=i
            return count
        if min(tests(True),tests(False))<2:return reject('untested_boundaries')
        outside=[float(c.close)>upper+.15*a or float(c.close)<lower-.15*a for c in prior[-12:]]
        if any(x and y for x,y in zip(outside,outside[1:])):return reject('range_not_holding')
        if not lower<entry<upper:return reject('range_broken')
        sign=1 if float(setup.low)<=lower+.25*a and float(setup.close)<=lower+.2*(upper-lower) else -1 if float(setup.high)>=upper-.25*a and float(setup.close)>=upper-.2*(upper-lower) else 0
        if not sign or not confirmed(sign):return reject('no_boundary_rebound')
        target=(upper+lower)/2
    elif name=='SWEEP_RECLAIM_V3':
        prior=c15[-20:-2];upper=max(float(c.high) for c in prior);lower=min(float(c.low) for c in prior)
        sign=1 if float(setup.low)<lower-.05*a and entry>lower+.15*a else -1 if float(setup.high)>upper+.05*a and entry<upper-.15*a else 0
        if not sign:return reject('no_confirmed_two_bar_sweep')
        # Wick through boundary must be reclaimed by the setup close itself.
        if not lower<float(setup.close)<upper:return reject('sweep_did_not_reclaim')
        if sign*(entry-float(setup.close))<=0 or sign*(entry-float(last.open))<=.1*a:return reject('reversal_not_confirmed')
        if sign*(entry-(lower if sign==1 else upper))>1.2*a:return reject('late_reclaim')
        if not lower<entry<upper:return reject('range_broken')
        if volume_ratio(c15[:-1])<1.1:return reject('weak_sweep_volume')
        if er24>.65:return reject('persistent_trend')
    else:
        # The separately tested hourly-trend/retest hypothesis was worse on the
        # 90d comparison and was rejected. Preserve original admission direction.
        original='MOMENTUM_CONTINUATION_V2' if name=='MOMENTUM_CONTINUATION_V3' else 'SESSION_RANGE_BREAKOUT_V1'
        plan,detail=control(original,symbol,c15,c1,c4);d.update(detail);d['strategy']=name
        if plan is None:return None,d
        sign=1 if plan.direction=='LONG' else -1
        width=float(last.high-last.low)
        location=(entry-float(last.low))/width if width else .5
        if sign*(entry-float(last.open))<.2*a or (location<.65 if sign==1 else location>.35):return reject('weak_breakout_close')
        distance=abs(plan.entry-plan.stop)
        d.update(reason='SIGNAL',reward_risk=2.,stop_atr=distance/a)
        return ResearchPlan(name,symbol,plan.direction,last.close_time,entry,plan.stop,entry+sign*2*distance,12),d

    extreme=min(float(c.low) for c in (setup,last)) if sign==1 else max(float(c.high) for c in (setup,last))
    if name=='RANGE_REBOUND_V3':extreme=min(extreme,lower) if sign==1 else max(extreme,upper)
    distance=max(minimum_stop*a,sign*(entry-extreme)+.15*a)
    if distance>3*a:return reject('structural_stop_too_far')
    stop=entry-sign*distance
    if target is None:target=entry+sign*2*distance
    reward=sign*(target-entry)/distance
    if min(stop,target)<=0 or reward<1.2:return reject('insufficient_target_space')
    d.update(reason='SIGNAL',reward_risk=reward,stop_atr=distance/a)
    return ResearchPlan(name,symbol,'LONG' if sign==1 else 'SHORT',last.close_time,entry,stop,target,RULES[name]['horizon_hours']),d


def expense_fraction(position,bps=5):
    """Full-horizon stop budget share due to fees, both slips and funding."""
    s=1 if position['direction']=='LONG' else -1
    if bps not in (5,10):raise ValueError('Only declared 5/10bps scenarios')
    stop=position['stop']*(1-s*bps/10000);entry=position['entry']
    reserve=(position['expires_ms']-position['opened_ms'])/28800000
    costs=(entry+stop)*.0004+abs(entry-position['entry_mid'])+abs(position['stop']-stop)+entry*.0001*reserve
    return costs*position['quantity']/position['risk_usdt']
