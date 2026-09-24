from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from test_early_paper import candidate,config
from trading_bot.models import Direction
from trading_bot.research.btc_rs_paper import (
    ARMS,RULE,BTCStore,BTCLab,btc_rs_decisions,self_benchmark_verified)


def source(direction=Direction.LONG):
    s=candidate(direction)
    return replace(s,metadata={**s.metadata,'squeeze_state':'release',
        'squeeze_entry_timing':'release_followthrough','squeeze_retest_confirmed':True,
        'relative_strength':{'alignment':'neutral','symbol_change_4h':'-.005',
                             'btc_change_4h':'-.005','relative_change_4h':'0'}})


@pytest.mark.parametrize('direction',[Direction.LONG,Direction.SHORT])
def test_only_self_rs_is_removed_without_faking_alignment(direction):
    s=source(direction);before=deepcopy(s.metadata)
    d=btc_rs_decisions(s,config().strategy)
    assert d['allowed']=={ARMS[0]:False,ARMS[1]:True}
    assert d['control_all_gate_failures']==['RS_NEUTRAL']
    assert d['treatment_all_gate_failures']==[]
    assert d['rs_applicability']=='not_applicable_self_benchmark'
    assert s.metadata==before and s.metadata['relative_strength']['alignment']=='neutral'


@pytest.mark.parametrize('patch',[{'btc_change_4h':None},{'relative_change_4h':'NaN'},
    {'symbol_change_4h':'0.01'},{'alignment':'aligned'},{'relative_change_4h':'.00001'}])
def test_invalid_or_mismatched_benchmark_cannot_bypass(patch):
    s=source();s.metadata['relative_strength'].update(patch)
    assert not any(btc_rs_decisions(s,config().strategy)['allowed'].values())


def test_decimal_rounding_and_non_btc_scope():
    s=source();s.metadata['relative_strength']['relative_change_4h']='-4e-29'
    assert self_benchmark_verified(s)
    s=replace(s,symbol='ETHUSDT')
    assert not self_benchmark_verified(s)
    assert not any(btc_rs_decisions(s,config().strategy)['allowed'].values())


@pytest.mark.parametrize('flag',['liquidation_cascade','adverse_liquidity_nearby',
    'structure_break_against','absorption_against','taker_flow_against'])
def test_other_safety_flow_structure_and_retest_guards_survive(flag):
    s=source();s.metadata['p8_order_flow'].update(score=.1,alignment='mixed',risk_flags=[flag],reasons=[])
    s.metadata['squeeze_retest_confirmed']=False
    d=btc_rs_decisions(s,config().strategy)
    assert not d['allowed'][ARMS[1]]
    assert 'RS_NEUTRAL' not in d['treatment_all_gate_failures']
    assert 'STRUCTURE_BREAK' in d['treatment_all_gate_failures']
    assert 'SQZ_RETEST_OR_STRONG_RELEASE' in d['treatment_all_gate_failures']
    assert set(d['control_all_gate_failures'])-set(d['treatment_all_gate_failures'])=={'RS_NEUTRAL'}


def test_context_missing_flow_and_early_rule_are_not_relaxed():
    s=source();s.metadata.update(regime='RANGE',squeeze_bars_4h=0)
    cfg=replace(config().strategy,squeeze_context_gate_enabled=True,
                squeeze_context_gate_require_4h_squeeze_or_trend=True)
    assert 'SQZ_CONTEXT' in btc_rs_decisions(s,cfg)['treatment_all_gate_failures']
    s=source();s.metadata['p8_order_flow']={'score':'NaN'}
    assert not any(btc_rs_decisions(s,cfg)['allowed'].values())
    s=source();s.metadata.update(squeeze_state='build',squeeze_entry_timing='early_breakout',squeeze_retest_confirmed=False)
    assert 'SQZ_RETEST_OR_STRONG_RELEASE' in btc_rs_decisions(s,config().strategy)['treatment_all_gate_failures']


def test_hourly_first_observation_restart_and_frozen_manifest(tmp_path):
    manifest={'cohort':'test','arms':ARMS,'policies':['FIRST_OBSERVATION'],'experiment':RULE}
    store=BTCStore(tmp_path,manifest);s=source();cfg=config().strategy
    profiles={arm:[(2.,1.,False,False)] for arm in ARMS}
    store.admit(s,3661000,btc_rs_decisions(s,cfg),{},profiles,'test')
    row=store.db.execute('SELECT * FROM positions').fetchone()
    assert row['arm']==ARMS[1] and row['entry_ms']==3720000
    assert row['source_id'].startswith('SQZ_BTC_RS:')
    store.db.close();store=BTCStore(tmp_path,manifest)
    assert store.admit(s,3662000,btc_rs_decisions(s,cfg),{},profiles,'test')==[]
    later=replace(s,metadata={**s.metadata,'source_hour_close_time':7199999,'observed_ms':7201000})
    result=store.admit(later,7201000,btc_rs_decisions(later,cfg),{},profiles,'test')
    assert any(r['result']=='SAME_SYMBOL_ACTIVE' for r in result)
    store.db.close()
    with pytest.raises(ValueError,match='Frozen'):BTCStore(tmp_path,{**manifest,'cohort':'changed'})


def test_lab_rejects_expanded_universe():
    with pytest.raises(ValueError,match='singleton'):BTCLab(config(),['BTCUSDT','ETHUSDT'],None,'test')
