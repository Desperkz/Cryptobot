import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from trading_bot.config import load_config
from trading_bot.models import Direction, Signal, TradingMode, TradingStyle
from trading_bot.research.mainnet_paper import ARMS, Lab, PublicDataClient, Store, gate_decisions
from trading_bot.research.paper_execution import simulate


def cfg():return load_config('config.yaml','.env.example')


def candidate():
    flow={'alignment':'aligned','score':.8,'risk_flags':[],
          'reasons':['structure_break_aligned'],'funding_rate':.0001}
    return Signal(symbol='BTCUSDT',direction=Direction.LONG,style=TradingStyle.INTRADAY,
        entry_price=Decimal('100'),stop_loss=Decimal('90'),take_profit=Decimal('124'),
        confidence=Decimal('.8'),reason='test',timeframe='15m',metadata={
        'strategy':'SQUEEZE_BREAKOUT','signal_bar_close_time':59999,'squeeze_bars_4h':2,
        'regime':'TREND_UP','squeeze_retest_confirmed':True,'order_flow':flow,'p8_order_flow':dict(flow),
        'relative_strength':{'alignment':'aligned','score':'.8'},'volume_ratio':'2'})


@pytest.mark.asyncio
async def test_public_client_blocks_all_private_and_write_routes_before_transport():
    c=PublicDataClient();calls=[]
    await c._client.aclose()
    c._client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:(calls.append(r),httpx.Response(200,json=[]))[1]))
    for method,path,signed in [('POST','/fapi/v1/order',True),('DELETE','/fapi/v1/order',True),
                               ('POST','/fapi/v1/listenKey',False),('GET','/fapi/v3/account',True),
                               ('GET','/fapi/v1/klines',True),('GET','/fapi/v1/order',False)]:
        with pytest.raises(PermissionError):await c._request(method,path,signed=signed)
    assert calls==[]
    await c.klines('BTCUSDT','1m',limit=1)
    assert len(calls)==1 and calls[0].method=='GET' and calls[0].url.host=='fapi.binance.com'
    assert 'X-MBX-APIKEY' not in calls[0].headers
    c.api_key='accidental-secret'
    with pytest.raises(PermissionError):await c.klines('BTCUSDT','1m')
    c.api_key=None;c.base_url='https://demo-fapi.binance.com'
    with pytest.raises(PermissionError):await c.klines('BTCUSDT','1m')
    await c.close()


def test_live_modes_cannot_construct_lab(tmp_path):
    for mode in [TradingMode.MAINNET_LIVE,TradingMode.TESTNET_LIVE]:
        with pytest.raises(ValueError):Lab(replace(cfg(),mode=mode),[],None,'test')


def test_first_observation_vs_first_admissible_and_restart_dedup(tmp_path):
    manifest={'cohort':'test','code':'frozen'};db=Store(tmp_path,manifest);s=candidate()
    profiles={a:[(2.,1.,False,False)] for a in ARMS}
    no={'allowed':{a:a=='BASELINE_2R' for a in ARMS}}
    db.admit(s,61000,no,{},profiles,'test')
    db.db.close();db=Store(tmp_path,manifest)
    yes={'allowed':{a:True for a in ARMS}}
    db.admit(s,75000,yes,{},profiles,'test');db.admit(s,76000,yes,{},profiles,'test')
    rows=[dict(r) for r in db.db.execute('SELECT * FROM positions')]
    assert len(rows)==5  # baseline twice; the other 3 only on first admissible
    assert all(r['entry_ms']==120000 for r in rows)
    assert not any(r['arm']=='CURRENT_GATE_PROFILE' and r['policy']=='FIRST_OBSERVATION' for r in rows)
    assert db.status()['observations']==3
    db.db.close()
    with pytest.raises(ValueError):Store(tmp_path,{**manifest,'code':'changed'})


def test_same_symbol_constraint_does_not_cross_arm_or_policy(tmp_path):
    db=Store(tmp_path,{'cohort':'x'});s=candidate();allow={'allowed':dict.fromkeys(ARMS,True)}
    profiles={a:[(2.,1.,False,False)] for a in ARMS}
    db.admit(s,61000,allow,{},profiles,'x')
    assert db.db.execute('SELECT count(*) FROM positions').fetchone()[0]==8
    other=replace(s,metadata={**s.metadata,'signal_bar_close_time':119999})
    outcomes=db.admit(other,121000,allow,{},profiles,'x')
    assert all(x['result']=='SAME_SYMBOL_ACTIVE' for x in outcomes)
    db.db.close()


def bar(o,h,l,c,t=0):return [t,str(o),str(h),str(l),str(c),'1',t+59999,'100']


def test_execution_never_realizes_unfinished_timeout():
    x=simulate([bar(100,101,99,100)],90,'LONG',[(2,1,False,False)])
    assert x['status']=='OPEN' and x['reason']=='mark_to_market' and x['R']<0
    assert simulate([bar(100,101,99,100)],90,'LONG',[(2,1,False,False)],complete=True)['status']=='CLOSED'


def test_stop_priority_gap_and_short_costs():
    x=simulate([bar(100,130,89,120)],90,'LONG',[(2,1,False,False)])
    assert x['status']=='CLOSED' and x['reason']=='stop' and x['R']<-1
    x=simulate([bar(100,101,99,100),bar(80,85,75,82,60000)],90,'LONG',[(2,1,False,False)])
    assert x['R']<-1.9
    x=simulate([bar(100,101,70,75)],110,'SHORT',[(2,1,False,False)])
    assert 1.9<x['R']<2
    assert simulate([bar(100,101,99,100)],110,'LONG',[(2,1,False,False)])['status']=='INVALID_ENTRY'


def test_partial_exit_conservative_breakeven_and_signed_funding():
    p=[(1,.25,True,False),(1.6,.35,False,True),(2.2,.4,False,True)]
    x=simulate([bar(100,112,99,111)],90,'LONG',p)
    assert x['reason']=='raised_stop' and 0<x['R']<.3
    assert simulate([bar(100,101,99,100)],110,'SHORT',p,funding_rate=.0001)['funding_R']<0


def test_gate_vectors_preserve_structural_rejections():
    s=candidate();dec=gate_decisions(s,cfg().strategy)
    assert all(dec['allowed'].values())
    flow={**s.metadata['order_flow'],'alignment':'against','score':.2}
    weak=replace(s,metadata={**s.metadata,'order_flow':flow,'p8_order_flow':flow})
    dec=gate_decisions(weak,cfg().strategy)
    assert not dec['allowed']['CURRENT_GATE_PROFILE'] and dec['allowed']['P8_OBSERVE_PROFILE']
    bad={**flow,'risk_flags':['absorption_against']}
    dec=gate_decisions(replace(weak,metadata={**weak.metadata,'p8_order_flow':bad}),cfg().strategy)
    assert not dec['allowed']['P8_OBSERVE_PROFILE'] and 'absorption_against' in dec['p8_all_gates']['structural_flags']


@pytest.mark.asyncio
async def test_monitor_recovers_pending_trade_and_caches_same_venue_candles(tmp_path):
    db=Store(tmp_path,{'cohort':'test'});s=candidate()
    now=__import__('time').time()*1000; observed=int(now)//60000*60000-180000
    db.admit(s,observed,{'allowed':{a:a=='BASELINE_2R' for a in ARMS}}, {},{a:[(2,1,False,False)] for a in ARMS},'test')
    class Feed:
        evidence={}
        async def klines(self,symbol,interval,limit,start_time,end_time):
            return [bar(100,130,89,120,t) for t in range(start_time,end_time+1,60000)]
    lab=Lab(cfg(),['BTCUSDT'],db,'test')
    await lab.monitor(Feed())
    assert {r[0] for r in db.db.execute('SELECT status FROM positions')}=={'CLOSED'}
    count=db.db.execute('SELECT count(*) FROM execution_candles').fetchone()[0]
    await lab.monitor(Feed())
    assert db.db.execute('SELECT count(*) FROM execution_candles').fetchone()[0]==count
    assert json.loads((tmp_path/'status.json').read_text())['order_capability'] is False
    await lab.client.close();db.db.close()
