from copy import deepcopy
from dataclasses import dataclass
import math
import pytest
from trading_bot.research import diversified_v4_gates as g
from trading_bot.research import diversified_v4_common as c
from trading_bot.strategy_engine.indicators import ema,atr
from trading_bot.models import Candle

AT=1800000000000


def bars(interval,stamp,n=300):
    end=(stamp+1)//interval*interval-1;start=end+1-n*interval
    return [[start+i*interval,str(100+i*.01),str(101+i*.01),str(99+i*.01),str(100.2+i*.01),'100',start+(i+1)*interval-1] for i in range(n)]


def fixture():
    stamp=AT+899999;observed=stamp+2000
    raw={'observed_ms':observed,'requested_ms':{'15m':observed,'1h':observed},'15m':bars(900000,stamp),'1h':bars(3600000,stamp)}
    plan={'id':1,'strategy':'MOMENTUM_CONTINUATION_V3','symbol':'BTCUSDT','direction':'LONG',
          'opened_ms':observed,'entry_mid':104.,'entry':104*1.0005,'stop':102.,'target':108.,'risk_usdt':2.,'expires_ms':observed+12*3600000,'snapshot_id':1}
    plan['quantity']=c.quantity(plan)
    snapshot={'id':1,'symbol':'BTCUSDT','bar_ms':stamp,'observed_ms':observed}
    return plan,snapshot,raw


def test_indicators_match_frozen_source_and_inputs_untouched():
    plan,snap,raw=fixture();before=deepcopy((plan,snap,raw));f=g.extract(plan,snap,raw)
    rows=[Candle.from_binance(b) for b in raw['1h']]
    assert f['h1_ema20']==ema([float(v.close) for v in rows],20)[-1]
    assert f['h1_atr14']==pytest.approx(atr(rows,14)[-1],rel=1e-13)
    assert (plan,snap,raw)==before


def test_unclosed_future_prices_do_not_affect_gate():
    plan,snap,raw=fixture();before=g.extract(plan,snap,raw)
    for key,interval in (('15m',900000),('1h',3600000)):
        last=raw[key][-1];start=last[0]+interval
        raw[key].append([start,'100000','200000','1','150000','999999',start+interval-1])
    assert g.extract(plan,snap,raw)==before


@pytest.mark.parametrize('mutation',[lambda r:r['1h'].pop(100),lambda r:r['15m'][-1].__setitem__(2,'NaN'),lambda r:r.update(observed_ms=1)])
def test_missing_invalid_and_mismatched_inputs_not_scored(mutation):
    p,s,r=fixture();mutation(r)
    with pytest.raises(ValueError):g.extract(p,s,r)


@pytest.mark.parametrize('side,trend,expected',[('LONG',1,True),('LONG',0,True),('LONG',-1,False),('SHORT',1,False),('SHORT',-1,True)])
def test_hourly_direction_is_not_an_outcome_based_long_ban(side,trend,expected):
    assert g.evaluate({'variant':'VWAP_REVERSION_V4_TREND_GUARD','direction':side,'hourly_trend':trend})['accepted']==expected


@pytest.mark.parametrize('side,close,expected',[('LONG',110,False),('LONG',110.01,True),('SHORT',90,False),('SHORT',89.99,True)])
def test_structure_needs_strict_close_past_opposite_extreme(side,close,expected):
    assert g.evaluate({'variant':'SWEEP_RECLAIM_V4_STRUCTURE','direction':side,'signal_close':close,'setup_high':110,'setup_low':90})['accepted']==expected


@pytest.mark.parametrize('value,expected',[(1.,True),(math.nextafter(1.,2.),False),(-2.,True)])
def test_extension_limit_is_predeclared_and_one_sided(value,expected):
    assert g.evaluate({'variant':'MOMENTUM_CONTINUATION_V4_EXTENSION','direction':'SHORT','signed_extension_atr_h1':value})['accepted']==expected


@pytest.mark.parametrize('strategy,hours',[('VWAP_REVERSION_V3_RECLAIM',8),('SWEEP_RECLAIM_V3',6),('MOMENTUM_CONTINUATION_V3',12)])
def test_each_original_horizon_and_cost_budget_validated(strategy,hours):
    p,s,r=fixture();p['strategy']=strategy;p['expires_ms']=p['opened_ms']+hours*3600000;p['quantity']=c.quantity(p)
    c.validate_plan(p,s,p['opened_ms']+1)
    when=(p['opened_ms']//60000+3)*60000-1
    result=c.expected_settlement(p,p['target'],when)
    row={'status':'CLOSED','closed_ms':when,'reason':'TARGET',**result}
    copied=c.validate_control(p,row,when+1)
    assert copied['net_usdt']==row['net_usdt']
    row['costs']['fees']+=.1
    with pytest.raises(ValueError,match='COST_MISMATCH'):c.validate_control(p,row,when+1)


def test_stress_keeps_two_dollar_budget_and_does_not_improve_target_profit():
    p,s,r=fixture();when=(p['opened_ms']//60000+3)*60000-1
    primary=c.expected_settlement(p,p['target'],when,5);stress=c.expected_settlement(p,p['target'],when,10)
    assert stress['net_usdt']<primary['net_usdt'] and stress['quantity']<primary['quantity']
    stop=c.expected_settlement(p,p['stop'],p['expires_ms'],10)
    assert stop['net_usdt']==pytest.approx(-2.)
