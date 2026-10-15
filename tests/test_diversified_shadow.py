import asyncio
from dataclasses import replace
import json
import math
from pathlib import Path

import pytest

from trading_bot.models import Candle
from trading_bot.research.diversified_shadow import PublicClient,advance,connect,fill_plan,admit,settle,decode,validate_settings,monitor,scan
from trading_bot.strategy_engine.diversified import DEFAULTS,ResearchPlan,evaluate,parameters

ROOT=Path(__file__).resolve().parents[1]
SETTINGS=json.loads((ROOT/'diversified_shadow_settings.json').read_text())
DAY=1792022400000

def candle(t,o,h,l,c,volume=100,tf=900000):
    from decimal import Decimal as D
    return Candle(t,D(str(o)),D(str(h)),D(str(l)),D(str(c)),D(str(volume)),t+tf-1)

def series(amplitude=1.):
    start=DAY+8*3600000-205*900000
    c15=[]
    for i in range(205):
        p=100+amplitude*math.sin(i*math.pi/4)
        c15.append(candle(start+i*900000,p-.05,p+.2,p-.2,p))
    end=c15[-1].close_time
    higher=lambda tf:[candle(end-(205-i)*tf+1,100,100.2,99.8,100+.05*math.sin(i*math.pi/2),tf=tf) for i in range(205)]
    return c15,higher(3600000),higher(14400000)

def reflected(candles):return [replace(c,open=200-c.open,high=200-c.low,low=200-c.high,close=200-c.close) for c in candles]

@pytest.mark.parametrize('name',list(DEFAULTS))
@pytest.mark.parametrize('short',[False,True])
def test_each_rule_has_a_reachable_long_and_short_setup(name,short):
    c15,c1,c4=series(2. if name=='RANGE_REBOUND_V2' else 1.)
    t=c15[-1].open_time
    if name in ('MEAN_REVERSION_V2','VWAP_REVERSION_V2'):
        c15[-2]=candle(t-900000,96,96.2,93.8,94,160)
        c15[-1]=candle(t,94.5,95.7,94.3,95.5,140)
    elif name=='RANGE_REBOUND_V2':
        c15[-2]=candle(t-900000,98.3,98.4,97.7,98,100)
        c15[-1]=candle(t,98.05,98.35,98,98.25,110)
    elif name=='SWEEP_RECLAIM_V2':
        c15[-2]=candle(t-900000,99,99.1,98.4,98.85,160)
        c15[-1]=candle(t,98.9,99.5,98.8,99.35,110)
    elif name=='MOMENTUM_CONTINUATION_V2':
        from trading_bot.strategy_engine.indicators import atr
        upper=max(float(c.high) for c in c15[-21:-1]);a=atr(c15,14)[-1]
        c15[-2]=candle(t-900000,upper-.2*a,upper,upper-.4*a,upper-.05*a)
        c15[-1]=candle(t,upper-.2*a,upper+.25*a,upper-.3*a,upper+.2*a,170)
        c4=[candle(c.open_time,80+i*.1,80.2+i*.1,79.8+i*.1,80+i*.1,tf=14400000) for i,c in enumerate(c4)]
    else:
        from trading_bot.strategy_engine.indicators import atr
        session=[c for c in c15[:-1] if DAY<=c.open_time<DAY+6*3600000]
        upper=max(float(c.high) for c in session);a=atr(c15,14)[-1]
        c15[-2]=candle(t-900000,upper-.3*a,upper-.05*a,upper-.4*a,upper-.1*a)
        c15[-1]=candle(t,upper-.1*a,upper+.3*a,upper-.2*a,upper+.2*a,170)
    if short:c15,c1,c4=map(reflected,(c15,c1,c4))
    plan,diag=evaluate(name,'BTCUSDT',c15,c1,c4)
    assert plan is not None,diag
    assert plan.direction==('SHORT' if short else 'LONG')
    assert (plan.stop>plan.entry>plan.target) if short else (plan.stop<plan.entry<plan.target)

def test_rejects_future_higher_timeframe_and_incomplete_session():
    c15,c1,c4=series()
    c4[-1]=replace(c4[-1],close_time=c15[-1].close_time+1)
    assert evaluate('VWAP_REVERSION_V2','BTCUSDT',c15,c1,c4)[1]['reason']=='future_higher_timeframe'
    c15,c1,c4=series();c15=[c for c in c15 if c.open_time!=DAY+3600000]
    assert evaluate('SESSION_RANGE_BREAKOUT_V1','BTCUSDT',c15,c1,c4)[1]['reason']=='insufficient_candles'

def test_invalid_threshold_and_session_configuration():
    with pytest.raises(ValueError):parameters('SESSION_RANGE_BREAKOUT_V1',{'range_end_utc':13})
    with pytest.raises(ValueError):parameters('VWAP_REVERSION_V2',{'max_efficiency':float('nan')})
    with pytest.raises(ValueError):validate_settings({**SETTINGS,'risk_usdt':150})

@pytest.mark.parametrize('method,path,signed',[('POST','/fapi/v1/order',False),('GET','/fapi/v1/account',True),('DELETE','/fapi/v1/allOpenOrders',False),('GET','/fapi/v1/klines',True)])
def test_public_client_cannot_reach_exchange_orders_or_signed_endpoints(method,path,signed):
    async def check():
        client=PublicClient()
        try:
            with pytest.raises(PermissionError):await client._request(method,path,signed=signed)
        finally:await client.close()
    asyncio.run(check())

def test_even_injected_credentials_are_refused():
    async def check():
        client=PublicClient();client.api_key='sentinel'
        try:
            with pytest.raises(PermissionError):await client.klines('BTCUSDT','15m')
        finally:await client.close()
    asyncio.run(check())

def plan():return ResearchPlan('MEAN_REVERSION_V2','BTCUSDT','LONG',999,100,98,104,8)

def test_cost_sizing_stop_budget_and_no_double_entry_slippage():
    fill,reason=fill_plan(plan(),100,120001,SETTINGS);assert reason is None
    assert fill['entry']==pytest.approx(100.05)
    final=settle(fill,98,120001+8*3600000,'STOP')
    assert final['net_usdt']==pytest.approx(-2)
    costs=json.loads(final['costs'])
    assert costs['entry_slippage']==pytest.approx(.05*fill['quantity'])
    assert final['net_usdt']==pytest.approx(costs['gross_before_exit_slippage']-costs['exit_slippage']-costs['fees']-costs['funding'])

def test_stop_first_gap_and_contiguous_minute_guards():
    fill,_=fill_plan(plan(),100,120001,SETTINGS)
    assert fill['next_ms']==180000
    both=[[180000,100,105,97,103,10,239999]]
    result,cursor=advance(fill,both)
    assert result['reason']=='STOP_FIRST_AMBIGUOUS' and result['net_usdt']<0 and cursor==240000
    gap=[[180000,95,100,94,96,10,239999]]
    assert advance(fill,gap)[0]['net_usdt']<-2
    with pytest.raises(ValueError):advance(fill,[[240000,100,101,99,100,10,299999]])

def test_frozen_manifest_duplicate_entry_and_registration(tmp_path):
    root=tmp_path/'code';(root/'src').mkdir(parents=True)
    (root/'config.yaml').write_text('config');(root/'settings.json').write_text(json.dumps(SETTINGS))
    db,manifest=connect(tmp_path/'data',SETTINGS,root,123)
    try:
        assert manifest['registered_ms']==123 and manifest['order_capability'] is False
        assert admit(db,plan(),100,120001,SETTINGS,1)=='OPENED'
        assert admit(db,plan(),100,120002,SETTINGS,1)=='POSITION_ALREADY_OPEN'
        db.commit()
    finally:db.close()
    (root/'config.yaml').write_text('changed')
    with pytest.raises(ValueError,match='Frozen cohort'):connect(tmp_path/'data',SETTINGS,root,124)


def test_partial_bar_fetched_before_close_never_becomes_closed_during_network_delay():
    c15,c1,c4=series()
    last=c15[-1]
    future=candle(last.open_time+900000,100,200,1,190)
    c15.append(future)
    def raw(candles):return [[c.open_time,str(c.open),str(c.high),str(c.low),str(c.close),str(c.volume),c.close_time] for c in candles]
    source={'15m':raw(c15),'1h':raw(c1),'4h':raw(c4),'book':{'bidPrice':'99.99','askPrice':'100.01'},'depth':{},'agg':[],
            'requested_ms':{'15m':future.close_time-1,'book':future.close_time+1}}
    decoded=decode(source,'BTCUSDT',future.close_time+2)
    assert decoded[0][-1]==last
    assert decoded[0][-1].high<future.high


def test_session_has_at_most_one_entry_per_direction_per_day(tmp_path):
    root=tmp_path/'code';(root/'src').mkdir(parents=True)
    (root/'config.yaml').write_text('x');(root/'settings.json').write_text(json.dumps(SETTINGS))
    db,_=connect(tmp_path/'data',SETTINGS,root,DAY)
    try:
        p=replace(plan(),strategy='SESSION_RANGE_BREAKOUT_V1',bar_close_ms=DAY+8*3600000)
        assert admit(db,p,100,DAY+8*3600000+1,SETTINGS,1)=='OPENED'
        db.execute("UPDATE positions SET status='CLOSED'")
        assert admit(db,p,100,DAY+10*3600000,SETTINGS,2)=='SESSION_ALREADY_TAKEN'
    finally:db.close()


def test_monitor_persists_stop_exit_with_its_costs(tmp_path):
    root=tmp_path/'code';(root/'src').mkdir(parents=True)
    (root/'config.yaml').write_text('x');(root/'settings.json').write_text(json.dumps(SETTINGS))
    db,_=connect(tmp_path/'data',SETTINGS,root,100)
    try:
        assert admit(db,plan(),100,120001,SETTINGS,1)=='OPENED';db.commit()
        class Client:
            async def klines(self,*args,**kwargs):return [[180000,100,101,97,98,10,239999]]
        asyncio.run(monitor(db,Client(),240001))
        row=dict(db.execute('SELECT * FROM positions').fetchone())
        assert row['status']=='CLOSED' and row['reason']=='STOP'
        assert row['net_usdt']<0 and json.loads(row['costs'])['fees']>0
        assert row['closed_ms']==239999
    finally:db.close()


def test_prospective_scan_skips_pre_registration_bars_and_deduplicates(tmp_path,monkeypatch):
    import trading_bot.research.diversified_shadow as lab
    from decimal import Decimal
    from types import SimpleNamespace
    root=tmp_path/'code';(root/'src').mkdir(parents=True)
    (root/'config.yaml').write_text('x');(root/'settings.json').write_text(json.dumps(SETTINGS))
    settings={**SETTINGS,'symbols':['BTCUSDT']};db,manifest=connect(tmp_path/'data',settings,root,10000000)
    bars,_,_=series();stamp=[9999999]
    async def source(*args):return {'observed_ms':stamp[0]+1}
    monkeypatch.setattr(lab,'snapshot',source)
    monkeypatch.setattr(lab,'decode',lambda *args:([replace(bars[-1],close_time=stamp[0])],[],[],SimpleNamespace(spread_bps=Decimal('1')),100))
    monkeypatch.setattr(lab,'baseline_rules',lambda config:{name:SimpleNamespace(generate=lambda *a:None) for name in lab.BASELINES})
    monkeypatch.setattr(lab,'evaluate',lambda name,*a,**kw:(replace(plan(),strategy=name,bar_close_ms=stamp[0]),{'reason':'SIGNAL'}))
    try:
        assert asyncio.run(scan(db,None,manifest,settings,None,10000000))==[]
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0]==0
        stamp[0]=10800000
        assert asyncio.run(scan(db,None,manifest,settings,None,10800001))==[]
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==11
        assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==6
        asyncio.run(scan(db,None,manifest,settings,None,10800002))
        assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==6
    finally:db.close()
