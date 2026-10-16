from dataclasses import replace
from decimal import Decimal

import pytest

from trading_bot.models import Candle
from trading_bot.strategy_engine.vwap_pullback import evaluate,VERSION
from trading_bot.strategy_engine.diversified import ResearchPlan
from trading_bot.research.strategy_replay import fill,execute,statistics
from trading_bot.research.diversified_shadow import advance,fill_plan

END=1800000000000//14400000*14400000


def candle(t,o,h,l,c,v=1,interval=900000):
    return Candle(t,*[Decimal(str(x)) for x in (o,h,l,c,v)],t+interval-1)


def frames(short=False):
    c15=[candle(END-(205-i)*900000,100,100.1,99.9,100) for i in range(205)]
    c15[-2]=candle(c15[-2].open_time,100,100.12,99.9,100.01)
    c15[-1]=candle(c15[-1].open_time,100.02,100.2,99.98,100.15,2)
    c1=[candle(END-(205-i)*3600000,90+i*.049,90.1+i*.049,89.9+i*.049,90+i*.049,interval=3600000) for i in range(205)]
    c4=[candle(END-(205-i)*14400000,100,101,99,100,interval=14400000) for i in range(205)]
    if short:
        def mirror(c):return replace(c,open=200-c.open,high=200-c.low,low=200-c.high,close=200-c.close)
        c15,c1,c4=([mirror(c) for c in cs] for cs in (c15,c1,c4))
    return c15,c1,c4


@pytest.mark.parametrize('short',[False,True])
def test_independent_closed_bar_pullback_has_structural_stop_and_fixed_target(short):
    a,b,c=frames(short);before=(list(a),list(b),list(c))
    plan,diag=evaluate('BTCUSDT',a,b,c)
    assert plan is not None and plan.strategy==VERSION
    assert plan.direction==('SHORT' if short else 'LONG') and diag['reason']=='SIGNAL'
    sign=-1 if short else 1
    assert sign*(plan.entry-plan.stop)>0 and sign*(plan.target-plan.entry)>0
    assert (plan.target-plan.entry)/(plan.entry-plan.stop)==pytest.approx(2)
    assert 1.2<=diag['stop_atr']<=2.5 and (a,b,c)==before


def test_requires_actual_vwap_area_retest():
    a,b,c=frames();p=a[-2]
    a[-2]=replace(p,open=p.open+2,high=p.high+2,low=p.low+2,close=p.close+2)
    assert evaluate('BTCUSDT',a,b,c)[1]['reason']=='no_vwap_retest'


def test_last_closed_bar_must_break_setup_extreme():
    a,b,c=frames();a[-1]=replace(a[-1],close=Decimal('100.1'))
    assert evaluate('BTCUSDT',a,b,c)[1]['reason']=='no_closed_bar_confirmation'


def test_confirmation_cannot_use_thin_volume():
    a,b,c=frames();a[-1]=replace(a[-1],volume=Decimal('.5'))
    assert evaluate('BTCUSDT',a,b,c)[1]['reason']=='weak_confirmation_volume'


@pytest.mark.parametrize('problem',['future','stale','gap','nan','partial','bad_ohlc','flat_trend'])
def test_bad_or_noncausal_data_never_generates_entry(problem):
    a,b,c=frames()
    if problem=='future':b=[replace(x,open_time=x.open_time+3600000,close_time=x.close_time+3600000) for x in b]
    elif problem=='stale':b=[replace(x,open_time=x.open_time-3600000,close_time=x.close_time-3600000) for x in b]
    elif problem=='gap':a[10]=replace(a[10],open_time=a[10].open_time-900000)
    elif problem=='nan':a[-1]=replace(a[-1],close=Decimal('NaN'))
    elif problem=='partial':a[-1]=replace(a[-1],close_time=a[-1].close_time+1)
    elif problem=='bad_ohlc':a[-1]=replace(a[-1],high=Decimal('90'))
    else:b=[replace(x,open=Decimal(100),high=Decimal(101),low=Decimal(99),close=Decimal(100)) for x in b]
    assert evaluate('BTCUSDT',a,b,c)[0] is None


@pytest.mark.parametrize('direction,mid,stop,target',[('LONG',100,98,104),('SHORT',100,102,96)])
def test_common_five_bps_model_preserves_frozen_v2_and_ten_bps_recomputes_budget(direction,mid,stop,target):
    plan=ResearchPlan(VERSION,'BTCUSDT',direction,0,mid,stop,target,8)
    settings={'risk_usdt':2.,'max_drift_r':.35,'min_net_rr':.8}
    five,_=fill(plan,mid,0,settings,5)
    original,_=fill_plan(plan,mid,0,settings)
    assert five==original
    ten,_=fill(plan,mid,0,settings,10)
    assert ten['quantity']<five['quantity']
    bar=[0,'100','103' if direction=='SHORT' else '101','99' if direction=='SHORT' else '97','100','1',59999]
    assert execute({**five,'next_ms':0},[bar],5)==advance({**original,'next_ms':0},[bar])[0]
    result=execute(ten,[bar],10)
    assert -2.00001<=result['net_usdt']<0 and result['reason']=='STOP'


def test_stress_replay_stops_first_on_ambiguous_bar_and_honors_gaps():
    p={'direction':'LONG','next_ms':0,'stop':98.,'target':104.,'entry':100.1,
        'entry_mid':100.,'quantity':1.,'opened_ms':0,'expires_ms':3600000}
    result=execute(p,[[0,'100','105','97','100','1',59999]],10)
    assert result['reason']=='STOP_FIRST_AMBIGUOUS'
    with pytest.raises(ValueError,match='MISSING_MINUTE'):
        execute(p,[[60000,'100','101','99','100','1',119999]],10)


def test_statistics_excludes_open_marks_and_uses_exit_time_for_closed_drawdown():
    rows=[{'status':'CLOSED','closed_ms':200,'symbol':'BTC','net_usdt':4.,'risk_usdt':2.},
        {'status':'CLOSED','closed_ms':100,'symbol':'SOL','net_usdt':-2.,'risk_usdt':2.},
        {'status':'OPEN','net_usdt':1000}]
    s=statistics(rows)
    assert s['closed']==2 and s['open']==1 and s['net_usdt']==2
    assert s['avg_r']==.5 and s['max_closed_drawdown_usdt']==-2 and s['profit_factor']==2


def test_simultaneous_closures_do_not_invent_an_intraminute_equity_path():
    rows=[{'status':'CLOSED','closed_ms':100,'symbol':'AAA','net_usdt':-8.,'risk_usdt':2.},
          {'status':'CLOSED','closed_ms':100,'symbol':'ZZZ','net_usdt':10.,'risk_usdt':2.}]
    assert statistics(rows)['max_closed_drawdown_usdt']==0
