"""Common cost-aware replay primitives for independent closed-bar hypotheses."""
import math
from itertools import groupby

from trading_bot.research.diversified_shadow import fill_plan, advance


def fill(plan, mid, now, settings, bps=5):
    if bps==5:
        return fill_plan(plan,mid,now,settings)
    if bps!=10:
        raise ValueError('Only declared 5/10bps scenarios supported')
    sign=1 if plan.direction=='LONG' else -1
    distance=abs(plan.entry-plan.stop)
    if not all(math.isfinite(v) and v>0 for v in (mid,plan.entry,plan.stop,plan.target)) or distance<=0:
        return None,'INVALID_PRICE'
    if abs(mid-plan.entry)/distance>settings['max_drift_r']:
        return None,'ENTRY_DRIFT'
    slip=bps/10000
    entry=mid*(1+sign*slip)
    stop_effective=plan.stop*(1-sign*slip)
    target_effective=plan.target*(1-sign*slip)
    funding=entry*.0001*plan.horizon_hours/8
    loss=sign*(entry-stop_effective)+(entry+stop_effective)*.0004+funding
    gain=sign*(target_effective-entry)-(entry+target_effective)*.0004-funding
    if min(sign*(entry-plan.stop),sign*(plan.target-entry))<=0:
        return None,'PRICE_CROSSED_STOP_OR_TARGET'
    if gain/loss<settings['min_net_rr']:
        return None,'INSUFFICIENT_NET_TARGET_SPACE'
    return {'strategy':plan.strategy,'symbol':plan.symbol,'direction':plan.direction,
        'opened_ms':now,'entry_mid':mid,'entry':entry,'stop':plan.stop,'target':plan.target,
        'quantity':settings['risk_usdt']/loss,'risk_usdt':settings['risk_usdt'],
        'expires_ms':now+plan.horizon_hours*3600000,'next_ms':now,'status':'OPEN'},None


def execute(position,bars,bps=5):
    if bps==5:
        return advance(position,bars)[0]
    if bps!=10:
        raise ValueError('Only declared 5/10bps scenarios supported')
    sign=1 if position['direction']=='LONG' else -1
    for i,b in enumerate(bars):
        if int(b[0])!=position['next_ms']+i*60000:
            raise ValueError('MISSING_MINUTE')
        o,h,l,c=map(float,b[1:5]);when=int(b[6])
        stop=l<=position['stop'] if sign==1 else h>=position['stop']
        target=h>=position['target'] if sign==1 else l<=position['target']
        if stop:
            price=min(o,position['stop']) if sign==1 else max(o,position['stop'])
            reason='STOP_FIRST_AMBIGUOUS' if target else 'STOP'
        elif target:
            price=position['target'];reason='TARGET'
        elif when>=position['expires_ms']:
            price=c;reason='TIMEOUT'
        else:
            continue
        effective=price*(1-sign*bps/10000);qty=position['quantity'];entry=position['entry']
        fees=(entry+effective)*qty*.0004
        funding=entry*qty*.0001*max(0,when-position['opened_ms'])/28800000
        return {'closed_ms':when,'exit_price':effective,'reason':reason,
            'net_usdt':sign*(effective-entry)*qty-fees-funding}
    return None


def statistics(trades):
    closed=sorted((r for r in trades if r['status']=='CLOSED'),key=lambda r:(r['closed_ms'],r['symbol']))
    total=peak=drawdown=0.
    # Minute OHLC does not reveal the order of simultaneous cross-symbol exits.
    for _,event in groupby(closed,key=lambda row:row['closed_ms']):
        total+=sum(row['net_usdt'] for row in event)
        peak=max(peak,total);drawdown=min(drawdown,total-peak)
    gain=sum(max(r['net_usdt'],0) for r in closed)
    loss=-sum(min(r['net_usdt'],0) for r in closed)
    return {'entries':len(trades),'closed':len(closed),'open':len(trades)-len(closed),
        'net_usdt':total,'avg_r':sum(r['net_usdt']/r['risk_usdt'] for r in closed)/len(closed) if closed else None,
        'profit_factor':gain/loss if loss else None,'max_closed_drawdown_usdt':drawdown,
        'wins':sum(r['net_usdt']>0 for r in closed),
        'symbols':sorted({r['symbol'] for r in closed})}
