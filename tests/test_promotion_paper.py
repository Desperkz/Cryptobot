from copy import deepcopy
from dataclasses import replace
import json
import pytest
from test_early_paper import candidate,config
from trading_bot.bot import _shadow_conditional_lab_variant,_shadow_conditional_lab_v2_variant
from trading_bot.research.promotion_paper import ARMS,RULE,TransferLab,TransferStore,transfer_decisions

def source():
    s=candidate();s.metadata.update(strategy='SQUEEZE_BREAKOUT_DYNAMIC_UPD',transfer_closed_frames_valid=True)
    return s

def test_memory_limited_pilot_cannot_accidentally_run_as_a_permanent_process(monkeypatch):
    from trading_bot.research.promotion_paper import periodic_main
    monkeypatch.setattr('sys.argv',['promotion','--config','missing.yaml'])
    with pytest.raises(ValueError,match='--once'):periodic_main()

def test_exact_existing_conditional_scoring_without_other_tests_or_c2_high_promotion():
    s=source();before=deepcopy(s.metadata);cfg=config().strategy
    _,v1=_shadow_conditional_lab_variant(s,cfg);_,v2=_shadow_conditional_lab_v2_variant(s,cfg)
    d=transfer_decisions(s,cfg)
    assert d['conditional_v1']==v1 and d['conditional_v2']==v2
    assert d['allowed']==dict(zip(ARMS,(True,v1['bucket']=='HIGH',v1['bucket']=='MID',v2['bucket']=='MID')))
    assert s.metadata==before and not any('C2_HIGH' in a for a in ARMS)

@pytest.mark.parametrize('patch',[{'transfer_closed_frames_valid':False},{'strategy':'SQUEEZE_BREAKOUT'},
 {'order_flow':None},{'p8_order_flow':{'alignment':'aligned','score':'NaN'}},{'breakout_atr':'NaN'}])
def test_invalid_sources_fail_closed(patch):
    s=source();s.metadata.update(patch)
    assert not any(transfer_decisions(s,config().strategy)['allowed'].values())

def test_hour_identity_shared_occupancy_frozen_manifest_and_same_execution_plans(tmp_path):
    s=source();manifest={'cohort':'transfer-test','arms':ARMS,'policies':['FIRST_OBSERVATION'],'experiment':RULE}
    store=TransferStore(tmp_path,manifest)
    try:
        d={'allowed':{a:True for a in ARMS}}
        store.admit(s,s.metadata['observed_ms'],d,{}, {a:[(2.,1.,False,False)] for a in ARMS},'transfer-test')
        rows=list(store.db.execute('SELECT * FROM positions'))
        assert len(rows)==4 and len({r['plan'] for r in rows})==1
        assert all(r['source_id'].startswith('SQZ_TRANSFER:') for r in rows)
        assert store.admit(s,s.metadata['observed_ms']+1000,d,{}, {},'transfer-test')==[]
    finally:store.db.close()
    with pytest.raises(ValueError,match='Frozen'):TransferStore(tmp_path,{**manifest,'cohort':'other'})

@pytest.mark.asyncio
async def test_monitor_records_cost_variants_and_extends_stress_coverage(tmp_path):
    s=source();manifest={'cohort':'transfer-test','arms':ARMS,'policies':['FIRST_OBSERVATION'],'experiment':RULE}
    store=TransferStore(tmp_path,manifest);t=s.metadata['observed_ms']
    store.admit(s,t,{'allowed':{a:True for a in ARMS}},{},{a:[(2.,1.,False,False)] for a in ARMS},'transfer-test')
    entry=list(store.db.execute('SELECT entry_ms FROM positions'))[0][0]
    from unittest.mock import patch
    class Feed:
        evidence={}
        async def klines(self,symbol,interval,limit,start_time,end_time):
            return [[start_time,'100','120.17' if start_time==entry else '121','99','100','1',start_time+59999,'100']]
    lab=TransferLab(config(),['BTCUSDT'],store,'transfer-test')
    try:
        with patch('trading_bot.research.mainnet_paper.time.time',return_value=(entry+60000)/1000):
            await lab.monitor(Feed())
        rows=list(store.db.execute('SELECT result FROM positions'))
        assert len(rows)==4
        assert all(json.loads(r[0])['cost_comparison']['10bps']['stop_only']['status']=='OPEN' for r in rows)
        with patch('trading_bot.research.mainnet_paper.time.time',return_value=(entry+120000)/1000):
            await lab.monitor(Feed())
        assert all(json.loads(r[0])['cost_comparison']['10bps']['stop_only']['status']=='CLOSED' for r in store.db.execute('SELECT result FROM positions'))
        assert store.status()['meta']['cost_monitor']['status']=='OK'
    finally:await lab.client.close();store.db.close()
