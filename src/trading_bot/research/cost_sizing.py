"""Isolated fixed-budget cost sizing and same-bar execution stress.

The frozen original paper_execution module is intentionally not modified.
"""
from math import isfinite


def simulate_sized(bars, stop, direction, targets, *, complete=False,
                   funding_rate=None, slippage_bps=5, cost_aware=False, budget=2.):
    if not bars:
        return {'status': 'PENDING'}
    if direction not in ('LONG', 'SHORT') or slippage_bps not in (5, 10):
        raise ValueError('Invalid execution scenario')
    if not isfinite(float(budget)) or budget <= 0 or not isfinite(float(stop)) or stop <= 0:
        raise ValueError('Invalid budget/stop')
    if funding_rate is not None and not isfinite(float(funding_rate)):
        raise ValueError('Invalid funding rate')
    start = int(bars[0][0])
    if len(bars) > 1440 or (complete and len(bars) != 1440):
        raise ValueError('Invalid 24h coverage')
    for i, b in enumerate(bars):
        o, h, l, c = map(float, b[1:5])
        if (int(b[0]) != start+i*60000 or int(b[6]) != int(b[0])+59999
                or not all(isfinite(v) and v > 0 for v in (o, h, l, c))
                or not l <= min(o, c) <= max(o, c) <= h):
            raise ValueError('Invalid minute bars')
    if not targets or abs(sum(t[1] for t in targets)-1.) > 1e-9:
        raise ValueError('Invalid target fractions')
    if any(not isfinite(float(t[0])) or t[0] <= 0 or not isfinite(float(t[1])) or t[1] <= 0 for t in targets):
        raise ValueError('Invalid targets')
    sign = 1 if direction == 'LONG' else -1
    adverse = slippage_bps/10000.; fee = .0004
    raw = float(bars[0][1]); entry = raw*(1+sign*adverse)
    distance = sign*(entry-stop)
    if distance <= 0:
        return {'status': 'INVALID_ENTRY', 'reason': 'stop_wrong_side_at_entry'}
    funding_used = sign*float(funding_rate) if funding_rate is not None else .0001
    # Entry slippage is already in entry-stop distance; do not add it twice.
    stop_fill = stop*(1-sign*adverse)
    reserve = stop*adverse+(entry+stop_fill)*fee+entry*max(0.,funding_used)*3
    qty = budget/(distance+reserve if cost_aware else distance)
    remaining=1.;gross=fees=slip=funding=0.;exit_ms=start
    pending=[{'p':entry+sign*distance*t[0],'f':t[1],'be':t[2],'tr':t[3],'done':False} for t in targets]
    trail=False;ambiguous=0

    def close(price, fraction, when):
        nonlocal remaining,gross,fees,slip,funding,exit_ms
        effective=price*(1-sign*adverse);q=qty*fraction
        gross+=sign*(price-entry)*q
        fees+=(entry+effective)*q*fee
        slip+=abs(price-effective)*q
        funding+=entry*q*funding_used*max(when-start,0)/28800000
        remaining=max(0.,remaining-fraction);exit_ms=when

    reason='timeout'
    for b in bars:
        o,h,l,c=map(float,b[1:5]);when=int(b[6])
        hit_stop=lambda p:l<=p if sign==1 else h>=p
        hit_target=lambda p:h>=p if sign==1 else l<=p
        if hit_stop(stop):
            ambiguous+=int(any(hit_target(t['p']) for t in pending if not t['done']))
            close(min(stop,o) if sign==1 else max(stop,o),remaining,when);reason='stop';break
        for t in pending:
            if t['done'] or not hit_target(t['p']):continue
            close(t['p'],min(t['f'],remaining),when);t['done']=True
            if remaining<=1e-12:reason='targets';break
            if t['be']:
                be=entry*(1+sign*.0002)
                stop=max(stop,be) if sign==1 else min(stop,be)
            if t['tr']:trail=True
            if hit_stop(stop):
                ambiguous+=1;close(stop,remaining,when);reason='raised_stop';break
        if remaining<=1e-12:break
        if trail:
            candidate=h*.996 if sign==1 else l*1.004
            stop=max(stop,candidate) if sign==1 else min(stop,candidate)
            if hit_stop(stop):ambiguous+=1;close(stop,remaining,when);reason='trail';break
    still_open=remaining>1e-12 and not complete
    if remaining>1e-12:close(float(bars[-1][4]),remaining,int(bars[-1][6]))
    net=gross-fees-slip-funding
    return {'status':'OPEN' if still_open else 'CLOSED','reason':'mark_to_market' if still_open else reason,
            'model_pnl_usdt':net,'R':net/budget,'gross_usdt':gross,'fees_usdt':fees,
            'exit_slippage_usdt':slip,'entry_slippage_usdt':abs(entry-raw)*qty,
            'funding_usdt':funding,'funding_source':'entry_rate_continuous_estimate' if funding_rate is not None else 'adverse_buffer',
            'entry_price':entry,'entry_ms':start,'last_ms':exit_ms,'quantity':qty,
            'stop_distance':distance,'expense_reserve_per_unit':reserve,
            'sizing':'STOP_PLUS_EXPENSES' if cost_aware else 'STOP_ONLY','budget_usdt':budget,
            'slippage_bps_each_side':slippage_bps,'ambiguous_bars':ambiguous}


def compare_sizing(bars, stop, direction, targets, *, complete=False, funding_rate=None, budget=2.):
    return {str(bps)+'bps':{name:simulate_sized(bars,stop,direction,targets,complete=complete,
                funding_rate=funding_rate,slippage_bps=bps,cost_aware=aware,budget=budget)
            for name,aware in (('stop_only',False),('with_expenses',True))} for bps in (5,10)}
