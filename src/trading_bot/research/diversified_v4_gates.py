"""Three predeclared, input-only admission changes to frozen V3 observations.

No outcome, networking, strategy fitting, direction/symbol ban or order code.
Uses only the source's saved, closed bars and observed entry midpoint.
"""
import hashlib
import json
import math
from decimal import Decimal

VARIANTS={
    'VWAP_REVERSION_V3_RECLAIM':'VWAP_REVERSION_V4_TREND_GUARD',
    'SWEEP_RECLAIM_V3':'SWEEP_RECLAIM_V4_STRUCTURE',
    'MOMENTUM_CONTINUATION_V3':'MOMENTUM_CONTINUATION_V4_EXTENSION',
}
RULES={
    'VWAP_REVERSION_V4_TREND_GUARD':{
        'change':'Reject an opposing hourly EMA20/EMA50 price-and-slope trend even when 6h efficiency is below the old conditional threshold.',
        'ema_fast':20,'ema_slow':50,'slope_bars':3,'reject':'direction * hourly_trend < 0'},
    'SWEEP_RECLAIM_V4_STRUCTURE':{
        'change':'Require the final closed confirmation candle beyond the sweep setup opposite extreme, not merely beyond its close.',
        'reject':'LONG close <= setup high; SHORT close >= setup low','strict_break':True},
    'MOMENTUM_CONTINUATION_V4_EXTENSION':{
        'change':'Refuse chasing a breakout beyond one hourly ATR from hourly EMA20 in the trade direction.',
        'ema_period':20,'atr_period':14,'maximum_signed_extension_atr':1.0},
}
HISTORICAL_CHECKS={
    'min_accepted_closed':12,'min_retained_fraction':.15,'min_accepted_utc_days':5,
    'positive_primary_net':True,'positive_paired_delta':True,'nonnegative_10bps_net':True,
    'nonnegative_both_calendar_halves':True,'nonnegative_without_best_trade':True,
    'automatic_paper_promotion':False,
}


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def integer(value):
    if isinstance(value,bool) or not isinstance(value,int) or value<0:raise ValueError('INVALID_TIMESTAMP_OR_ID')
    return value


def number(value):
    if isinstance(value,bool):raise ValueError('INVALID_NUMBER')
    value=float(value)
    if not math.isfinite(value):raise ValueError('NONFINITE_NUMBER')
    return value


def ema(values,period):
    alpha=2/(period+1);result=[values[0]]
    for value in values[1:]:result.append(value*alpha+result[-1]*(1-alpha))
    return result


def closed_series(raw,interval,until):
    if not isinstance(raw,list) or len(raw)>1000:raise ValueError('INVALID_CANDLE_LIST')
    rows=[]
    for row in raw:
        if not isinstance(row,list) or len(row)<7:raise ValueError('INVALID_CANDLE_ROW')
        start=integer(row[0]);end=integer(row[6])
        if start%interval or end!=start+interval-1:raise ValueError('INVALID_CANDLE_TIME')
        values=[number(x) for x in row[1:6]]
        o,h,l,c,v=values
        if not 0<l<=min(o,c)<=max(o,c)<=h or v<0:raise ValueError('INVALID_CANDLE_PRICE')
        if end>until:continue
        if rows and start!=rows[-1][0]+interval:raise ValueError('CANDLE_GAP')
        rows.append([start,*values,end])
    if len(rows)<205:raise ValueError('INSUFFICIENT_CLOSED_CANDLES')
    if until-rows[-1][6]>=interval:raise ValueError('STALE_CLOSED_CANDLES')
    return rows


def extract(plan,snapshot,raw):
    """Exclude still-open/future bars exactly as the original source decoder."""
    observed=integer(plan['opened_ms']);stamp=integer(snapshot['bar_ms'])
    if plan['strategy'] not in VARIANTS or plan['direction'] not in ('LONG','SHORT'):
        raise ValueError('UNSUPPORTED_SOURCE_PLAN')
    if integer(snapshot['observed_ms'])!=observed or integer(raw['observed_ms'])!=observed or stamp>=observed:
        raise ValueError('SOURCE_TIME_MISMATCH')
    requested=raw.get('requested_ms',{})
    c15=closed_series(raw['15m'],900000,min(observed,integer(requested.get('15m',observed))))
    if c15[-1][6]!=stamp:raise ValueError('SOURCE_SIGNAL_BAR_MISMATCH')
    c1=closed_series(raw['1h'],3600000,min(stamp,integer(requested.get('1h',observed))))
    prices=[c[4] for c in c1]
    fast=ema(prices,20);slow=ema(prices,50)
    ranges=[float(Decimal(str(c1[0][2]))-Decimal(str(c1[0][3])))]
    for before,current in zip(c1,c1[1:]):
        ranges.append(max(current[2]-current[3],abs(current[2]-before[4]),abs(current[3]-before[4])))
    hourly_atr=ema(ranges,14)[-1]
    if hourly_atr<=0:raise ValueError('ZERO_HOURLY_ATR')
    trend=1 if prices[-1]>fast[-1]>slow[-1] and fast[-1]>fast[-4] else -1 if prices[-1]<fast[-1]<slow[-1] and fast[-1]<fast[-4] else 0
    sign=1 if plan['direction']=='LONG' else -1
    mid=number(plan['entry_mid'])
    if mid<=0:raise ValueError('INVALID_ENTRY_MIDPOINT')
    return {'source_strategy':plan['strategy'],'variant':VARIANTS[plan['strategy']],
            'symbol':plan['symbol'],'direction':plan['direction'],'signal_bar_ms':stamp,
            'observed_ms':observed,'hour_close_ms':c1[-1][6],
            'hourly_trend':trend,'h1_ema20':fast[-1],'h1_ema50':slow[-1],
            'h1_ema20_three_bars_ago':fast[-4],'h1_atr14':hourly_atr,
            'signal_close':c15[-1][4],'setup_high':c15[-2][2],'setup_low':c15[-2][3],
            'signed_extension_atr_h1':sign*(mid-fast[-1])/hourly_atr,
            'closed_15m_bars':len(c15),'closed_1h_bars':len(c1)}


def evaluate(features):
    name=features['variant'];direction=features['direction']
    if name not in RULES or direction not in ('LONG','SHORT'):raise ValueError('INVALID_GATE_IDENTITY')
    sign=1 if direction=='LONG' else -1
    if name=='VWAP_REVERSION_V4_TREND_GUARD':
        trend=features['hourly_trend']
        if isinstance(trend,bool) or trend not in (-1,0,1):raise ValueError('INVALID_TREND')
        allowed=sign*trend>=0;reason='HOURLY_TREND_PASS' if allowed else 'OPPOSING_HOURLY_TREND'
    elif name=='SWEEP_RECLAIM_V4_STRUCTURE':
        boundary=number(features['setup_high'] if sign==1 else features['setup_low'])
        allowed=sign*(number(features['signal_close'])-boundary)>0
        reason='SETUP_EXTREME_BROKEN' if allowed else 'NO_STRUCTURAL_CONFIRMATION'
    else:
        extension=number(features['signed_extension_atr_h1'])
        allowed=extension<=RULES[name]['maximum_signed_extension_atr']
        reason='EXTENSION_PASS' if allowed else 'LATE_HOURLY_EXTENSION'
    return {'decision':'ACCEPT' if allowed else 'REJECT','accepted':allowed,
            'reason':reason,'variant':name,'features_sha256':digest(features)}
