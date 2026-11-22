"""Two fixed VWAP exit hypotheses. Original RANGE minute executor stays frozen."""
from copy import deepcopy

from .range_profit_execution import MINUTE,advance_control,advance_protected,validate_bars

RULES={
    'VWAP_TARGET_1R':{'target':'Closer of original anchor and effective_entry + direction * initial_price_R',
                      'stop':'ORIGINAL','horizon':'ORIGINAL','quantity':'ORIGINAL'},
    'VWAP_PROTECT_1R':{'arm':'Completed minute CLOSE at +1 initial price R',
                       'effective':'NEXT_FULL_MINUTE','stop_after_arm':'ORIGINAL_EFFECTIVE_ENTRY',
                       'target':'ORIGINAL','horizon':'ORIGINAL','quantity':'ORIGINAL'},
}
CHECKS={'min_closed_pairs':12,'min_coverage_fraction':.9,'min_utc_days':5,
        'positive_primary_net':True,'positive_paired_delta':True,'nonnegative_stress10_net':True,
        'nonnegative_both_calendar_halves':True,'nonnegative_net_without_best_trade':True,
        'nonnegative_delta_without_best_improvement':True,'automatic_paper_promotion':False}


def initial_state(plan):
    return {'next_ms':(plan['opened_ms']//MINUTE+1)*MINUTE,'armed_ms':None}


def closer_target(plan):
    sign=1 if plan['direction']=='LONG' else -1
    one_r=plan['entry']+sign*abs(plan['entry']-plan['stop'])
    target=min(plan['target'],one_r) if sign==1 else max(plan['target'],one_r)
    return {**plan,'target':target}


def replay(plan,bars):
    """All arms consume the same contiguous full-minute path; no plan mutations."""
    first=initial_state(plan)['next_ms']
    if not bars:raise ValueError('MISSING_MINUTES')
    rows=validate_bars(bars,first,bars[-1][6])
    control,_=advance_control({**plan,'next_ms':first},rows)
    target,_=advance_control({**closer_target(plan),'next_ms':first},rows)
    protected,state=advance_protected(plan,initial_state(plan),rows)
    return {'control':control,'VWAP_TARGET_1R':target,'VWAP_PROTECT_1R':protected,'protected_state':state}


def diagnostics(plan,bars,control):
    """Minute-close opportunities before control exit; wick extremes are diagnostic only."""
    side=1 if plan['direction']=='LONG' else -1;r=abs(plan['entry']-plan['stop'])
    before=[b for b in bars if b[6]<control['closed_ms']]
    closes=[side*(float(b[4])-plan['entry'])/r for b in before]
    return {'completed_minutes_before_exit':len(before),
            'best_close_r_before_exit':max(closes,default=None),
            'closed_above_1r_before_exit':any(v>=1 for v in closes),
            'closed_above_half_r_before_exit':any(v>=.5 for v in closes)}
