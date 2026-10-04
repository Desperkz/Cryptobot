import json
from copy import deepcopy
from dataclasses import replace

import pytest

from test_early_paper import candidate, config
from trading_bot.models import Direction, MarketRegime
from trading_bot.research.direction_paper import (
    ARMS, RULE, DirectionLab, DirectionStore, direction_decisions)
from trading_bot.research.mainnet_paper import main


@pytest.mark.parametrize('regime', list(MarketRegime))
@pytest.mark.parametrize('direction', [Direction.LONG, Direction.SHORT])
def test_direction_matrix_changes_only_two_opposite_pairs(regime, direction):
    s=candidate(direction);s.metadata['regime']=regime.value
    before=deepcopy(s.metadata);d=direction_decisions(s)
    opposite=(regime==MarketRegime.TREND_UP and direction==Direction.SHORT) or (
        regime==MarketRegime.TREND_DOWN and direction==Direction.LONG)
    assert d['allowed']=={ARMS[0]:True,ARMS[1]:not opposite}
    assert d['directional_gate_failures']==(['COUNTERTREND_4H'] if opposite else [])
    assert s.metadata==before


@pytest.mark.parametrize('regime', [None, '', 'trend_up', ' TREND_UP', 'NaN', [], 1])
def test_invalid_regime_fails_closed_in_both_arms(regime):
    s=candidate();s.metadata['regime']=regime
    assert not any(direction_decisions(s)['allowed'].values())
    assert 'INVALID_REGIME' in direction_decisions(s)['source_quality_failures']


def test_missing_regime_wrong_strategy_direction_and_metadata():
    s=candidate();s.metadata.pop('regime')
    assert not any(direction_decisions(s)['allowed'].values())
    s=candidate();s.metadata['strategy']='P8_SQUEEZE'
    assert not any(direction_decisions(s)['allowed'].values())
    assert not any(direction_decisions(replace(candidate(),direction=Direction.NONE))['allowed'].values())
    assert not any(direction_decisions(replace(candidate(),metadata=None))['allowed'].values())


def test_control_is_explicit_source_baseline_not_another_strict_vector():
    s=candidate();s.metadata.update(regime='RANGE',squeeze_retest_confirmed=False)
    s.metadata['p8_order_flow'].update(alignment='against',score=0,risk_flags=['liquidation_cascade'])
    before=deepcopy(s.metadata)
    assert all(direction_decisions(s)['allowed'].values())
    assert s.metadata==before


def manifest():
    return {'cohort':'test','arms':ARMS,'policies':['FIRST_OBSERVATION'],'experiment':RULE}


def test_equal_plans_hour_identity_no_retry_and_shared_occupancy(tmp_path):
    s=candidate();store=DirectionStore(tmp_path,manifest())
    profiles={arm:[(2.,1.,False,False)] for arm in ARMS}
    def admit(sig):
        return store.admit(sig,sig.metadata['observed_ms'],direction_decisions(sig),{},profiles,'test')
    admit(s);rows=list(store.db.execute('SELECT * FROM positions'))
    assert len(rows)==2 and len({r['plan'] for r in rows})==1
    assert {r['entry_ms'] for r in rows}=={3720000}
    assert all(r['source_id']=='SQZ_DIRECTION:BTCUSDT:LONG:3599999' for r in rows)
    store.db.close();store=DirectionStore(tmp_path,manifest())
    assert admit(s)==[]
    with store.db:store.db.execute('UPDATE positions SET status=? WHERE arm=?',('CLOSED',ARMS[1]))
    later=replace(s,metadata={**s.metadata,'source_hour_close_time':7199999,'observed_ms':7201000})
    assert all(r['result']=='SAME_SYMBOL_ACTIVE' for r in admit(later))
    with store.db:store.db.execute("UPDATE positions SET status='CLOSED'")
    assert admit(later)==[]
    store.db.close()
    with pytest.raises(ValueError,match='Frozen'):DirectionStore(tmp_path,{**manifest(),'cohort':'changed'})


def test_rejected_treatment_is_never_retried_after_regime_changes(tmp_path):
    s=candidate(Direction.SHORT);s.metadata['regime']='TREND_UP'
    store=DirectionStore(tmp_path,manifest());profiles={a:[(2.,1.,False,False)] for a in ARMS}
    result=store.admit(s,3661000,direction_decisions(s),{},profiles,'test')
    assert [r['result'] for r in result]==['PENDING_NEXT_MINUTE','GATE_REJECTED']
    s.metadata['regime']='TREND_DOWN'
    assert store.admit(s,3662000,direction_decisions(s),{},profiles,'test')==[]
    assert store.db.execute('SELECT count(*) FROM positions').fetchone()[0]==1
    store.db.close()


def test_entry_point_rejects_other_experiment_before_creating_data(tmp_path,monkeypatch):
    p=tmp_path/'settings.json';p.write_text(json.dumps({'execution_mode':'LOCAL_PAPER_ONLY',
        'market_data_base_url':'https://fapi.binance.com','experiment':'btc-rs-applicability-v1'}))
    monkeypatch.setattr('sys.argv',['lab','--config','config.yaml','--settings',str(p),'--data-dir',str(tmp_path/'data')])
    with pytest.raises(ValueError,match='entry point'):
        main(lab_class=DirectionLab,store_class=DirectionStore,experiment=RULE)
    assert not (tmp_path/'data').exists()


@pytest.mark.asyncio
async def test_paired_monitor_accounts_for_same_costs(tmp_path):
    store=DirectionStore(tmp_path,manifest());s=candidate()
    store.admit(s,3661000,direction_decisions(s),{},{a:[(2.,1.,False,False)] for a in ARMS},'test')
    class Feed:
        evidence={}
        async def klines(self,symbol,interval,limit,start_time,end_time):
            return [[start_time,'100','130','89','120','1',start_time+59999,'100']]
    lab=DirectionLab(config(),['BTCUSDT'],store,'test')
    try:
        await lab.monitor(Feed())
        rows=list(store.db.execute('SELECT status,result FROM positions'))
        assert all(r['status']=='CLOSED' for r in rows)
        assert len({r['result'] for r in rows})==1
        result=json.loads(rows[0]['result'])
        assert result['R'] < -1 and result['fees_R']>0 and result['exit_slippage_R']>0
    finally:
        await lab.client.close();store.db.close()
