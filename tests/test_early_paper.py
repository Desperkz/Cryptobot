import json
from dataclasses import replace
from decimal import Decimal

import pytest

from trading_bot.config import load_config
from trading_bot.models import Signal, Direction, TradingStyle
from trading_bot.research.early_paper import ARMS, RULE, EarlyLab, EarlyStore, early_decisions
from trading_bot.research.mainnet_paper import gate_decisions
from trading_bot.research.mainnet_paper import main


def config():
    return load_config('config.yaml', '.env.example')


def candidate(direction=Direction.LONG):
    flow={'alignment':'aligned','score':.8,'risk_flags':[],
          'reasons':['structure_break_aligned'],'funding_rate':.0001}
    return Signal(symbol='BTCUSDT',direction=direction,style=TradingStyle.INTRADAY,
        entry_price=Decimal('100'),stop_loss=Decimal('90') if direction==Direction.LONG else Decimal('110'),
        take_profit=Decimal('120') if direction==Direction.LONG else Decimal('80'),
        confidence=Decimal('.8'),reason='fixture',timeframe='15m',metadata={
            'strategy':'SQUEEZE_BREAKOUT','squeeze_state':'build','squeeze_entry_timing':'early_breakout',
            'squeeze_bars':8,'regime':'RANGE','squeeze_retest_confirmed':False,
            'breakout_atr':'.5','volume_ratio':'2','order_flow':dict(flow),'p8_order_flow':flow,
            'relative_strength':{'alignment':'aligned'},'signal_bar_close_time':3599999,
            'source_hour_close_time':3599999,'observed_ms':3661000})


def manifest():
    return {'cohort':'test','arms':ARMS,'policies':['FIRST_OBSERVATION'],'experiment':RULE}


@pytest.mark.parametrize('direction', [Direction.LONG, Direction.SHORT])
def test_early_entry_is_reachable_but_original_gate_stays_blocked(direction):
    s=candidate(direction);cfg=config().strategy
    result=early_decisions(s,cfg)
    assert result['allowed']==dict(zip(ARMS,[True,False,True]))
    assert result['early_all_gate_failures']==[]
    assert result['current_all_gate_failures']==['SQZ_RETEST_OR_STRONG_RELEASE']
    assert not gate_decisions(s,cfg)['allowed']['CURRENT_GATE_2R']
    assert not s.metadata['squeeze_retest_confirmed']  # No fabricated retest.


@pytest.mark.parametrize('flag', ['liquidation_cascade','adverse_liquidity_nearby',
                                  'structure_break_against','absorption_against'])
def test_structural_risk_blocks_treatment(flag):
    s=candidate();s.metadata['p8_order_flow']['risk_flags']=[flag]
    assert not early_decisions(s,config().strategy)['allowed'][ARMS[2]]


@pytest.mark.parametrize('alignment', ['neutral','against',''])
def test_relative_strength_is_preserved(alignment):
    s=candidate();s.metadata['relative_strength']['alignment']=alignment
    assert not early_decisions(s,config().strategy)['allowed'][ARMS[2]]


def test_corrected_flow_structure_and_score_guards():
    s=candidate();s.metadata['p8_order_flow']['reasons']=[]
    assert 'STRUCTURE_BREAK' in early_decisions(s,config().strategy)['early_all_gate_failures']
    s=candidate();s.metadata['p8_order_flow']['alignment']='against'
    assert not early_decisions(s,config().strategy)['allowed'][ARMS[2]]
    s=candidate();s.metadata['p8_order_flow'].update(alignment='mixed',score=.1)
    assert not early_decisions(s,config().strategy)['allowed'][ARMS[2]]
    s=candidate();s.metadata['p8_order_flow']['score']='NaN'
    assert not any(early_decisions(s,config().strategy)['allowed'].values())


@pytest.mark.parametrize('key,value', [('breakout_atr','NaN'),('breakout_atr','3'),
    ('volume_ratio','.5'),('squeeze_bars',1),('p8_order_flow',None),('squeeze_state','release')])
def test_invalid_source_cannot_enter(key,value):
    s=candidate();s.metadata[key]=value
    assert not any(early_decisions(s,config().strategy)['allowed'].values())


def test_hour_dedup_restart_shared_occupancy_and_equal_entries(tmp_path):
    cfg=config();store=EarlyStore(tmp_path,manifest());s=candidate()
    profiles={arm:[(2.,1.,False,False)] for arm in ARMS}
    def admit(sig):
        return store.admit(sig,sig.metadata['observed_ms'],early_decisions(sig,cfg.strategy),{},profiles,'test')
    admit(s)
    rows=list(store.db.execute('SELECT * FROM positions'))
    assert len(rows)==2 and {r['entry_ms'] for r in rows}=={3720000}
    assert len({r['plan'] for r in rows})==1  # Same entry, stop, costs, sizing, targets.
    later=replace(s,metadata={**s.metadata,'signal_bar_close_time':4499999,'observed_ms':4501000})
    assert admit(later)==[]
    store.db.close();store=EarlyStore(tmp_path,manifest())
    assert admit(later)==[]
    with store.db:
        store.db.execute('UPDATE positions SET status=? WHERE arm=?',('CLOSED',ARMS[0]))
    next_hour=replace(s,metadata={**s.metadata,'source_hour_close_time':7199999,
                                 'signal_bar_close_time':7199999,'observed_ms':7201000})
    outcomes=admit(next_hour)
    assert all(o['result']=='SAME_SYMBOL_ACTIVE' for o in outcomes if o['arm']!=ARMS[1])
    with store.db:store.db.execute("UPDATE positions SET status='CLOSED'")
    assert admit(next_hour)==[]  # No retrospective retry of occupied/rejected source.
    store.db.close()
    with pytest.raises(ValueError):EarlyStore(tmp_path,{**manifest(),'cohort':'changed'})


def test_invalid_hour_identity_rejected(tmp_path):
    store=EarlyStore(tmp_path,manifest());s=candidate()
    for stamp in [None,3599998,7199999]:
        with pytest.raises(ValueError):store.source_id(replace(s,metadata={**s.metadata,'source_hour_close_time':stamp}))
    store.db.close()


@pytest.mark.parametrize('entry_early', [False, True])
def test_entry_point_rejects_settings_for_other_experiment(tmp_path, monkeypatch, entry_early):
    settings=tmp_path/'settings.json'
    settings.write_text(json.dumps({'execution_mode':'LOCAL_PAPER_ONLY',
        'market_data_base_url':'https://fapi.binance.com',
        **({} if entry_early else {'experiment':RULE['name']})}))
    monkeypatch.setattr('sys.argv',['lab','--config','config.yaml','--settings',str(settings),
                                  '--data-dir',str(tmp_path/'data')])
    with pytest.raises(ValueError,match='entry point'):
        if entry_early:main(lab_class=EarlyLab,store_class=EarlyStore,experiment=RULE)
        else:main()
    assert not (tmp_path/'data').exists()


@pytest.mark.asyncio
async def test_new_arm_monitor_uses_existing_paper_execution(tmp_path):
    store=EarlyStore(tmp_path,manifest());s=candidate()
    store.admit(s,s.metadata['observed_ms'],early_decisions(s,config().strategy),{},
                {arm:[(2.,1.,False,False)] for arm in ARMS},'test')
    class Feed:
        evidence={}
        async def klines(self,symbol,interval,limit,start_time,end_time):
            return [[start_time,'100','130','89','120','1',start_time+59999,'100']]
    lab=EarlyLab(config(),['BTCUSDT'],store,'test')
    try:
        await lab.monitor(Feed())
        rows=list(store.db.execute('SELECT status,result FROM positions'))
        assert all(r['status']=='CLOSED' for r in rows)
        assert len({r['result'] for r in rows})==1
        assert json.loads(rows[0]['result'])['R'] < -1
    finally:
        await lab.client.close();store.db.close()
