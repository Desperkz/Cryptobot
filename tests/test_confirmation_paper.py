import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from test_early_paper import candidate, config
from trading_bot.models import Candle, Direction
from trading_bot.research.confirmation_paper import (
    ARMS, RULE, ConfirmationLab, ConfirmationStore, ConfirmationStrategy,
    confirmation_decisions, level_confirmation)
from trading_bot.research.mainnet_paper import main


def bar(start, duration, o='99', h='100', l='98', c='99'):
    return Candle(open_time=start, close_time=start+duration-1,
                  open=Decimal(o), high=Decimal(h), low=Decimal(l),
                  close=Decimal(c), volume=Decimal('1'), quote_volume=Decimal('100'))


def fixture(direction=Direction.LONG):
    h1=[bar(i*3600000,3600000) for i in range(24)]
    h1[-1]=bar(23*3600000,3600000,'99','105','98','102')
    m15=[bar(i*900000,900000) for i in range(96)]
    # Distinct price breakout, post-breakout pullback/rejection, then holding bars.
    m15[-4]=bar(23*3600000,900000,'99','102','99','101')
    m15[-3]=bar(23*3600000+900000,900000,'100','103','100','102')
    m15[-2]=bar(23*3600000+1800000,900000,'102','104','102','103')
    m15[-1]=bar(23*3600000+2700000,900000,'103','104','101','102')
    if direction==Direction.SHORT:
        def mirror(c):return replace(c,open=Decimal('198')-c.open,high=Decimal('198')-c.low,
            low=Decimal('198')-c.high,close=Decimal('198')-c.close)
        h1=list(map(mirror,h1));m15=list(map(mirror,m15))
    s=candidate(direction);observed=24*3600000+1000
    s=replace(s,entry_price=h1[-1].close,metadata={**s.metadata,'squeeze_state':'release',
        'squeeze_entry_timing':'release_followthrough','squeeze_release_offset':0,
        'compression_high':'100','compression_low':'98','source_hour_close_time':h1[-1].close_time,
        'signal_bar_close_time':m15[-1].close_time,'observed_ms':observed})
    return s,h1,m15,observed


def attach(s,h1,m15,t):
    return replace(s,metadata={**s.metadata,'level_confirmation':level_confirmation(s,h1,m15,config().strategy,t)})


@pytest.mark.parametrize('direction',[Direction.LONG,Direction.SHORT])
def test_price_breakout_precedes_distinct_same_level_retest(direction):
    s,h1,m15,t=fixture(direction);before=deepcopy(s.metadata)
    e=level_confirmation(s,h1,m15,config().strategy,t)
    assert e['status']=='VALID' and e['confirmed_retest']
    assert e['breakout_close_ms']==m15[-4].close_time
    assert e['retest_close_ms']==m15[-3].close_time
    assert e['hourly_level_held'] and e['latest_15m_level_held']
    assert s.metadata==before
    d=confirmation_decisions(attach(s,h1,m15,t),config().strategy)
    assert d['allowed']=={ARMS[0]:True,ARMS[1]:False,ARMS[2]:True}


def test_first_price_breakout_is_not_its_own_retest_even_with_native_annotation():
    s,h1,m15,t=fixture()
    for i in range(4):m15[-4+i]=bar((92+i)*900000,900000)
    m15[-1]=bar(95*900000,900000,'99','104','99','102')
    s.metadata['squeeze_retest_confirmed']=True
    d=confirmation_decisions(attach(s,h1,m15,t),config().strategy)
    assert d['allowed']=={ARMS[0]:True,ARMS[1]:True,ARMS[2]:False}
    assert d['level_evidence']['retest_close_ms'] is None


def test_breach_resets_confirmation_and_latest_hour_must_hold():
    s,h1,m15,t=fixture();m15[-2]=bar(94*900000,900000,'102','103','98','99')
    e=level_confirmation(s,h1,m15,config().strategy,t)
    assert not e['confirmed_retest'] and e['breakout_close_ms']==m15[-1].close_time
    s,h1,m15,t=fixture();h1[-1]=bar(23*3600000,3600000,'99','105','98','99')
    e=level_confirmation(s,h1,m15,config().strategy,t)
    assert e['confirmed_retest'] and not e['hourly_level_held']
    assert not confirmation_decisions(attach(s,h1,m15,t),config().strategy)['allowed'][ARMS[2]]


def test_shared_strong_exception_keeps_original_thresholds_but_requires_held_level():
    s,h1,m15,t=fixture();s.metadata.update(breakout_atr='1.50',squeeze_retest_confirmed=False)
    for i in range(4):m15[-4+i]=bar((92+i)*900000,900000,'101','104','101','102')
    d=confirmation_decisions(attach(s,h1,m15,t),config().strategy)
    assert all(d['allowed'].values()) and d['shared_strong_release']
    s.metadata['p8_order_flow']['score']=.71
    assert not confirmation_decisions(attach(s,h1,m15,t),config().strategy)['shared_strong_release']
    s.metadata['p8_order_flow'].update(score=.8,risk_flags=['book_imbalance_against'])
    assert not confirmation_decisions(attach(s,h1,m15,t),config().strategy)['shared_strong_release']


@pytest.mark.parametrize('change',['future','gap','bad_ohlc','nan','wrong_level','missing_level','bad_offset','partial_bar','stale','wrong_direction'])
def test_invalid_or_unavailable_evidence_fails_closed(change):
    s,h1,m15,t=fixture()
    if change=='future':t=m15[-1].close_time
    if change=='gap':m15.pop(-2)
    if change=='bad_ohlc':m15[-1]=replace(m15[-1],low=Decimal('200'))
    if change=='nan':h1[-1]=replace(h1[-1],close=Decimal('NaN'))
    if change=='wrong_level':s.metadata['compression_high']='101'
    if change=='missing_level':s.metadata.pop('compression_high')
    if change=='bad_offset':s.metadata['squeeze_release_offset']=True
    if change=='partial_bar':m15[-1]=replace(m15[-1],close_time=m15[-1].close_time-1)
    if change=='stale':t+=900000
    if change=='wrong_direction':s=replace(s,direction=Direction.NONE)
    e=level_confirmation(s,h1,m15,config().strategy,t)
    assert e['status']=='INVALID'
    assert not any(confirmation_decisions(replace(s,metadata={**s.metadata,'level_confirmation':e}),config().strategy)['allowed'].values())


def test_malformed_evidence_or_flow_never_bypasses_confirmation():
    s,h1,m15,t=fixture();s=attach(s,h1,m15,t)
    for patch in [{'confirmed_retest':'true'},{'retest_close_ms':s.metadata['level_confirmation']['breakout_close_ms']},
                  {'as_of_ms':t+1},{'level':'NaN'}]:
        broken=replace(s,metadata={**s.metadata,'level_confirmation':{**s.metadata['level_confirmation'],**patch}})
        assert not any(confirmation_decisions(broken,config().strategy)['allowed'].values())
    for patch in [{'score':'NaN'},{'score':2},{'risk_flags':None},{'reasons':[{}]}]:
        broken=replace(s,metadata={**s.metadata,'p8_order_flow':{**s.metadata['p8_order_flow'],**patch}})
        assert not any(confirmation_decisions(broken,config().strategy)['allowed'].values())


def test_build_anchor_is_valid_without_fabricating_release_or_native_retest():
    s,h1,m15,t=fixture();s.metadata.update(squeeze_state='build',squeeze_entry_timing='early_breakout',squeeze_release_offset=None)
    s=attach(s,h1,m15,t)
    assert confirmation_decisions(s,config().strategy)['allowed'][ARMS[2]]
    assert not s.metadata['squeeze_retest_confirmed'] and s.metadata['squeeze_state']=='build'


def manifest():return {'cohort':'test','arms':ARMS,'policies':['FIRST_OBSERVATION'],'experiment':RULE}


def test_first_hourly_observation_shared_occupancy_and_identical_plans(tmp_path):
    s,h1,m15,t=fixture();s=attach(s,h1,m15,t);s.metadata['squeeze_retest_confirmed']=True
    store=ConfirmationStore(tmp_path,manifest());profiles={a:[(2.,1.,False,False)] for a in ARMS}
    store.admit(s,t,confirmation_decisions(s,config().strategy),{},profiles,'test')
    rows=list(store.db.execute('SELECT * FROM positions'))
    assert len(rows)==3 and len({r['plan'] for r in rows})==1
    assert all(r['source_id'].startswith('SQZ_CONFIRM:') for r in rows)
    store.db.close();store=ConfirmationStore(tmp_path,manifest())
    assert store.admit(s,t+1000,confirmation_decisions(s,config().strategy),{},profiles,'test')==[]
    later=replace(s,metadata={**s.metadata,'source_hour_close_time':25*3600000-1,'observed_ms':25*3600000+1000})
    assert all(r['result']=='SAME_SYMBOL_ACTIVE' for r in store.admit(later,25*3600000+1000,{'allowed':{a:True for a in ARMS}},{},profiles,'test'))
    store.db.close()
    with pytest.raises(ValueError,match='Frozen'):ConfirmationStore(tmp_path,{**manifest(),'cohort':'other'})


def test_wrapper_preserves_generator_signal_and_attaches_closed_evidence(monkeypatch):
    s,h1,m15,t=fixture();before=deepcopy(s.metadata)
    monkeypatch.setattr('trading_bot.research.confirmation_paper.time.time',lambda:t/1000)
    monkeypatch.setattr('trading_bot.research.confirmation_paper.SqueezeBreakoutStrategy.generate',lambda *args:s)
    wrapper=ConfirmationStrategy(config().strategy,None);actual=wrapper.generate('BTCUSDT',m15,h1,[],None)
    assert s.metadata==before and actual.entry_price==s.entry_price and actual.stop_loss==s.stop_loss
    assert actual.metadata['level_confirmation']['confirmed_retest']


def test_later_retest_does_not_rewrite_first_hourly_refusal(tmp_path):
    s,h1,m15,t=fixture()
    for i in range(4):m15[-4+i]=bar((92+i)*900000,900000)
    m15[-1]=bar(95*900000,900000,'99','104','99','102')
    s=attach(s,h1,m15,t)
    store=ConfirmationStore(tmp_path,manifest());profiles={a:[(2.,1.,False,False)] for a in ARMS}
    try:
        store.admit(s,t,confirmation_decisions(s,config().strategy),{},profiles,'test')
        later=bar(96*900000,900000,'100','103','100','102')
        m15.append(later);later_t=later.close_time+1000
        later_s=replace(s,metadata={**s.metadata,'observed_ms':later_t})
        later_s=attach(later_s,h1,m15,later_t)
        assert confirmation_decisions(later_s,config().strategy)['allowed'][ARMS[2]]
        assert store.admit(later_s,later_t,confirmation_decisions(later_s,config().strategy),{},profiles,'test')==[]
        assert [r['arm'] for r in store.db.execute('SELECT arm FROM positions')]==[ARMS[0]]
    finally:store.db.close()


def test_only_the_registered_release_anchor_supplies_a_retest_window():
    s,h1,m15,t=fixture()
    s.metadata['squeeze_release_offset']=1
    e=level_confirmation(s,h1,m15,config().strategy,t)
    assert e['status']=='VALID' and e['anchor_open_ms']==22*3600000
    # The prior hour's expansion still is not a price break; current first price
    # break cannot count as its own retest, even with a saved native 1h retest.
    for i in range(4):m15[-4+i]=bar((92+i)*900000,900000)
    m15[-1]=bar(95*900000,900000,'99','104','99','102')
    s.metadata['squeeze_retest_confirmed']=True
    d=confirmation_decisions(attach(s,h1,m15,t),config().strategy)
    assert d['allowed'][ARMS[1]] and not d['allowed'][ARMS[2]]


def test_wrong_experiment_cannot_create_data(tmp_path,monkeypatch):
    p=tmp_path/'settings.json';p.write_text(json.dumps({'execution_mode':'LOCAL_PAPER_ONLY',
        'market_data_base_url':'https://fapi.binance.com','experiment':'sqz-direction-v1'}))
    monkeypatch.setattr('sys.argv',['lab','--config','config.yaml','--settings',str(p),'--data-dir',str(tmp_path/'data')])
    with pytest.raises(ValueError,match='entry point'):main(lab_class=ConfirmationLab,store_class=ConfirmationStore,experiment=RULE)
    assert not (tmp_path/'data').exists()


@pytest.mark.asyncio
async def test_monitor_retains_paired_execution_and_costs(tmp_path):
    s,h1,m15,t=fixture();s=attach(s,h1,m15,t);s.metadata['squeeze_retest_confirmed']=True
    store=ConfirmationStore(tmp_path,manifest())
    store.admit(s,t,confirmation_decisions(s,config().strategy),{},{a:[(2.,1.,False,False)] for a in ARMS},'test')
    class Feed:
        evidence={}
        async def klines(self,symbol,interval,limit,start_time,end_time):
            return [[start_time,'102','130','89','120','1',start_time+59999,'100']]
    lab=ConfirmationLab(config(),['BTCUSDT'],store,'test')
    try:
        await lab.monitor(Feed());rows=list(store.db.execute('SELECT status,result FROM positions'))
        assert len(rows)==3 and all(r['status']=='CLOSED' for r in rows)
        assert len({r['result'] for r in rows})==1
        assert json.loads(rows[0]['result'])['R'] < -1
    finally:
        await lab.client.close();store.db.close()
