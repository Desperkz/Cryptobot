from math import isclose
import pytest
from trading_bot.research.cost_sizing import compare_sizing,simulate_sized
from trading_bot.research.paper_execution import simulate

def bar(i,o=100,h=101,l=99,c=100):return [i*60000,str(o),str(h),str(l),str(c),'1',i*60000+59999,'100']
SINGLE=[(2.,1.,False,False)]
PROFILE=[(1.,.25,True,False),(1.7,.35,False,True),(2.3,.4,False,True)]

@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('scenario',['stop','target','gap','ambiguous','timeout','open','partials'])
def test_stop_only_exactly_matches_frozen_engine(direction,scenario):
    stop=98 if direction=='LONG' else 102
    bars=[bar(0)];complete=False;targets=SINGLE
    if scenario=='stop':bars=[bar(0,h=103,l=97)]
    if scenario=='target':bars=[bar(0,h=106 if direction=='LONG' else 101,l=99 if direction=='LONG' else 94)]
    if scenario=='gap':bars=[bar(0),bar(1,o=97 if direction=='LONG' else 103,h=104,l=96)]
    if scenario=='ambiguous':bars=[bar(0,h=106,l=94)]
    if scenario=='timeout':bars=[bar(i) for i in range(1440)];complete=True
    if scenario=='partials':bars=[bar(0,h=106,l=99) if direction=='LONG' else bar(0,h=101,l=94)];targets=PROFILE
    original=simulate(bars,stop,direction,targets,complete=complete,funding_rate=.0001)
    actual=simulate_sized(bars,stop,direction,targets,complete=complete,funding_rate=.0001)
    assert actual['status']==original['status'] and actual['reason']==original['reason']
    assert isclose(actual['R'],original['R'],rel_tol=1e-10,abs_tol=1e-10)
    assert actual['last_ms']==original['last_ms'] and actual['ambiguous_bars']==original['ambiguous_bars']

@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('funding',[None,0,.0002,-.0002])
def test_estimated_stop_budget_includes_costs_without_double_entry_slippage(direction,funding):
    stop=98 if direction=='LONG' else 102
    bars=[bar(i) for i in range(1439)]+[bar(1439,h=103,l=97)]
    results=compare_sizing(bars,stop,direction,SINGLE,complete=True,funding_rate=funding)
    for bps,pair in results.items():
        old,new=pair['stop_only'],pair['with_expenses']
        assert old['model_pnl_usdt'] < -2
        assert new['model_pnl_usdt'] >= -2-1e-9
        assert new['quantity']<old['quantity']
        assert new['entry_price']==old['entry_price'] and new['last_ms']==old['last_ms']
        assert isclose(new['model_pnl_usdt']/old['model_pnl_usdt'],new['quantity']/old['quantity'])

def test_gap_is_not_falsely_capped_at_estimated_budget():
    r=simulate_sized([bar(0),bar(1,o=90,h=91,l=89,c=90)],98,'LONG',SINGLE,cost_aware=True)
    assert r['reason']=='stop' and r['model_pnl_usdt'] < -2

def test_stress_recomputes_entry_targets_and_quantity_instead_of_flat_deduction():
    r=compare_sizing([bar(0,h=104.17,l=99)],98,'LONG',SINGLE)
    assert r['5bps']['stop_only']['status']=='CLOSED'
    assert r['10bps']['stop_only']['status']=='OPEN'
    assert r['10bps']['stop_only']['entry_price']>r['5bps']['stop_only']['entry_price']

@pytest.mark.parametrize('bad',['nan','gap','partial','ohlc','false_complete','bad_funding','bad_fraction'])
def test_malformed_or_incomplete_evidence_never_becomes_closed_result(bad):
    bars=[bar(0),bar(1)];kw={};targets=SINGLE
    if bad=='nan':bars[1][4]='NaN'
    if bad=='gap':bars[1][0]=120000
    if bad=='partial':bars[1][6]=100000
    if bad=='ohlc':bars[1][2]='98'
    if bad=='false_complete':kw['complete']=True
    if bad=='bad_funding':kw['funding_rate']=float('nan')
    if bad=='bad_fraction':targets=[(2.,.7,False,False)]
    with pytest.raises(ValueError):simulate_sized(bars,98,'LONG',targets,**kw)
