"""Offline attribution of recorded diversified-shadow trades.

Minute inputs are supplemental evidence; replay equality is checked explicitly.
No exchange access, admissions, settings mutations or strategy promotion.
"""
import collections
import json
import math

from trading_bot.research.diversified_shadow import decode, advance
from trading_bot.strategy_engine.diversified import vwap
from trading_bot.strategy_engine.indicators import ema, closes, atr
from trading_bot.strategy_engine.vwap_v3 import admission


def aggregate(rows):
    closed = [r for r in rows if r['status']=='CLOSED']
    net = sum(r['net_usdt'] for r in closed)
    cost = {k:sum(r['costs'][k] for r in closed) for k in
            ['fees','funding','entry_slippage','exit_slippage','gross_before_exit_slippage']}
    # gross_before_exit_slippage already includes adverse entry slippage.
    price_only = cost['gross_before_exit_slippage']+cost['entry_slippage']
    expenses = sum(cost[k] for k in ['fees','funding','entry_slippage','exit_slippage'])
    if not math.isclose(price_only-expenses, net, abs_tol=1e-8):
        raise ValueError('COST_RECONCILIATION_FAILED')
    gain = sum(max(r['net_usdt'],0) for r in closed)
    loss = -sum(min(r['net_usdt'],0) for r in closed)
    return {'entries':len(rows), 'closed':len(closed), 'open':len(rows)-len(closed),
        'wins':sum(r['net_usdt']>0 for r in closed), 'net_usdt':net,
        'avg_r':sum(r['net_usdt']/r['risk_usdt'] for r in closed)/len(closed) if closed else None,
        'profit_factor':gain/loss if loss else None, 'price_only_usdt':price_only,
        'expenses_usdt':expenses, 'costs':cost,
        'reasons':dict(collections.Counter(r['reason'] for r in closed))}


def analyze(evidence, minutes):
    trades = []
    for position in evidence['positions']:
        p = dict(position)
        if p['direction'] not in ('LONG','SHORT') or p['risk_usdt']<=0 or p['quantity']<=0:
            raise ValueError('INVALID_POSITION')
        raw = evidence['snapshots'][str(p['snapshot_id'])]
        c15,c1,c4,_,_ = decode(raw,p['symbol'],raw['observed_ms'])
        sign = 1 if p['direction']=='LONG' else -1
        a1,a15 = atr(c1,14)[-1],atr(c15,14)[-1]
        e1,e4,slow4 = ema(closes(c1),50),ema(closes(c4),50),ema(closes(c4),200)[-1]
        diag = next(json.loads(o['detail']) for o in evidence['observations']
            if o['snapshot_id']==p['snapshot_id'] and o['strategy']==p['strategy'])
        first = (p['opened_ms']//60000+1)*60000
        until = p['closed_ms'] or evidence['at_ms']//60000*60000-1
        bars = [b for b in minutes[p['symbol']] if first<=int(b[0]) and int(b[6])<=until]
        if not bars or int(bars[0][0])!=first:
            raise ValueError('MISSING_ENTRY_MINUTE')
        for i,b in enumerate(bars):
            o,h,l,c = map(float,b[1:5])
            if (int(b[0])!=first+i*60000 or int(b[6])!=int(b[0])+59999
                    or not all(math.isfinite(v) and v>0 for v in (o,h,l,c))
                    or not l<=min(o,c)<=max(o,c)<=h):
                raise ValueError('INVALID_OR_MISSING_MINUTE')
        outcome,_ = advance({**p,'next_ms':first},bars)
        if p['status']=='CLOSED' and (outcome is None or outcome['closed_ms']!=p['closed_ms']
                or outcome['reason']!=p['reason'] or not math.isclose(outcome['net_usdt'],p['net_usdt'],abs_tol=1e-8)):
            raise ValueError('SAVED_EXIT_REPLAY_MISMATCH')
        if p['status']=='OPEN' and outcome is not None:
            raise ValueError('OPEN_POSITION_ALREADY_CLOSED_IN_SUPPLEMENT')
        def excursion(rows):
            return (max([0.]+[(float(b[2])-p['entry'] if sign==1 else p['entry']-float(b[3]))*p['quantity']/p['risk_usdt'] for b in rows]),
                min([0.]+[(float(b[3])-p['entry'] if sign==1 else p['entry']-float(b[2]))*p['quantity']/p['risk_usdt'] for b in rows]))
        mfe,mae = excursion(bars)
        mfe_before_exit,_ = excursion(bars[:-1] if p['status']=='CLOSED' else bars)
        row = {k:p[k] for k in ['id','strategy','symbol','direction','opened_ms','closed_ms','status',
            'entry','stop','target','net_usdt','reason','risk_usdt','quantity']}
        row.update(diag=diag,mfe_r_upper=mfe,mfe_r_before_exit_minute=mfe_before_exit,
            mae_r_bar_bound=mae,minutes=len(bars),replay_delta_usdt=outcome['net_usdt']-p['net_usdt'] if outcome else None,
            costs=json.loads(p['costs']) if p['costs'] else None,
            against_1h=sign*(float(c1[-1].close)-e1[-1])<0,
            against_4h_stack=sign*(e4[-1]-slow4)<0,
            slope_1h_signed_atr=sign*(e1[-1]-e1[-7])/a1,
            vwap_slope_signed_atr=sign*(vwap(c15[-96:])-vwap(c15[-100:-4]))/a15,
            body_atr=sign*(float(c15[-1].close)-float(c15[-1].open))/a15,
            v3_diagnostic=admission(p['direction'],c1,c15[-1].close_time))
        trades.append(row)
    groups = {s:aggregate([r for r in trades if r['strategy']==s]) for s in sorted({r['strategy'] for r in trades})}
    vwap_rows = [r for r in trades if r['strategy']=='VWAP_REVERSION_V2']
    return {'at_ms':evidence['at_ms'], 'groups':groups,'trades':trades,
        'vwap_v3_subset':aggregate([r for r in vwap_rows if r['v3_diagnostic']['allowed']]),
        'vwap_slices':{k:{str(v):aggregate([r for r in vwap_rows if r[k]==v]) for v in {r[k] for r in vwap_rows}}
            for k in ['direction','against_1h','against_4h_stack']},
        'scope':'Offline diagnostic. Supplemental minutes, first full minute after entry; MFE/MAE intrabar bounds, no unknown price ordering reconstructed. V3 is post-hoc and NOT promoted.'}
