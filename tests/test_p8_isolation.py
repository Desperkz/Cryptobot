import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_bot.bot import (
    TradingBot, _conditional_shadow_source_seen, _order_flow_entry_rejection_reason,
    _p8_shadow_variants, _shadow_conditional_lab_variant, _shadow_conditional_lab_v2_variant,
    _shadow_conditional_profile, _squeeze_context_gate_rejection,
)
from trading_bot.config import ConfigError, load_config
from trading_bot.models import Direction, Signal, TradingMode, TradingStyle
from trading_bot.strategy_engine.order_flow import OrderFlowAnnotator


def config():
    return load_config('config.yaml', '.env.example')


def signal(source='SQUEEZE_BREAKOUT'):
    flow = {'alignment': 'aligned', 'score': '0.8', 'risk_flags': [],
            'reasons': ['structure_break_aligned'], 'open_interest_change_pct': '1'}
    return Signal(symbol='BTCUSDT', direction=Direction.LONG, style=TradingStyle.INTRADAY,
                  entry_price=Decimal('100'), stop_loss=Decimal('95'), take_profit=Decimal('111'),
                  confidence=Decimal('0.8'), reason='test', timeframe='15m', metadata={
                      'strategy': source, 'regime': 'TREND_UP', 'squeeze_bars_4h': 2,
                      'squeeze_retest_confirmed': True, 'signal_bar_close_time': 1789380000000,
                      'relative_strength': {'alignment': 'aligned', 'score': '0.80'},
                      'volume_ratio': '2', 'order_flow': deepcopy(flow), 'p8_order_flow': deepcopy(flow),
                  })


@pytest.mark.parametrize('mode', ['strict', 'measure', 'observe'])
@pytest.mark.parametrize('score', ['0.69', '0.70', '0.99'])
@pytest.mark.parametrize('flag', ['liquidation_cascade', 'structure_break_against', 'adverse_liquidity_nearby', 'absorption_against'])
def test_structural_flags_block_regardless_of_score(mode, score, flag):
    s=signal();s.metadata['order_flow'].update(score=score,risk_flags=[flag])
    assert _order_flow_entry_rejection_reason(s,replace(config().strategy,order_flow_entry_gate_mode=mode))


@pytest.mark.parametrize('regime', ['UNKNOWN','LOW_VOLATILITY','HIGH_VOLATILITY','','TREND_TYPO'])
def test_context_rejects_non_directional_regimes_without_compression(regime):
    s=signal();s.metadata.update(regime=regime,squeeze_bars_4h=0)
    assert _squeeze_context_gate_rejection(s,replace(config().strategy,squeeze_context_gate_enabled=True))


@pytest.mark.parametrize('regime', ['TREND_UP','TREND_DOWN','MOMENTUM'])
def test_context_explicit_directional_allowlist(regime):
    s=signal();s.metadata.update(regime=regime,squeeze_bars_4h=0)
    assert _squeeze_context_gate_rejection(s,replace(config().strategy,squeeze_context_gate_enabled=True)) is None


def test_global_observe_and_config_bypasses_are_blocked():
    cfg=config()
    for strategy in [
        replace(cfg.strategy,order_flow_entry_gate_mode='observe',squeeze_context_gate_enabled=True),
        replace(cfg.strategy,order_flow_entry_gate_mode='observe',squeeze_context_gate_enabled=True,
                squeeze_context_gate_require_4h_squeeze_or_trend=False),
        replace(cfg.strategy,shadow_conditional_neutralize_order_flow=True),
        replace(cfg.strategy,p8_shadow_cohort=cfg.strategy.shadow_conditional_lab_v2_cohort),
        replace(cfg.strategy,p8_shadow_risk_cap_pct=Decimal('.003')),
        replace(cfg.strategy,strategy_modes={**cfg.strategy.strategy_modes,'P8_SQZ_OBSERVE_SHADOW':'paper'}),
    ]:
        with pytest.raises(ConfigError):replace(cfg,strategy=strategy).validate()


def test_old_control_variants_ignore_neutralization_switch():
    s=signal('SQUEEZE_BREAKOUT_DYNAMIC_UPD');cfg=config().strategy
    s.metadata['order_flow'].update(alignment='against',score='0.2',risk_flags=['taker_flow_against'])
    for factory in [_shadow_conditional_lab_variant,_shadow_conditional_lab_v2_variant]:
        old,old_profile=factory(s,cfg)
        new,new_profile=factory(s,replace(cfg,shadow_conditional_neutralize_order_flow=True))
        assert old_profile==new_profile
        assert old.metadata['measurement_shadow']==new.metadata['measurement_shadow']


@pytest.mark.parametrize('flag', ['liquidation_cascade','adverse_liquidity_nearby','structure_break_against','absorption_against'])
def test_neutralized_v1_keeps_structural_penalties(flag):
    s=signal('SQUEEZE_BREAKOUT_DYNAMIC_UPD');s.metadata['order_flow']['risk_flags']=[flag]
    p=_shadow_conditional_profile(s,replace(config().strategy,shadow_conditional_neutralize_order_flow=True))
    assert p['components']['hostile_flags']<0
    assert p['score_version']!='conditional_context_v1'


@pytest.mark.parametrize('source', ['SQUEEZE_BREAKOUT','SQUEEZE_BREAKOUT_DYNAMIC_UPD'])
def test_p8_pairs_share_source_and_exits_but_not_cohort_or_score_version(source):
    s=signal(source);before=deepcopy(s);cfg=config().strategy
    variants,decisions=_p8_shadow_variants(s,cfg)
    assert len(variants)==2
    assert s==before
    a,b=[v.metadata['measurement_shadow'] for v in variants]
    assert a['source_cluster_id']==b['source_cluster_id']
    assert a['cohort']!=b['cohort']
    assert a['conditional_profile']['score_version']!=b['conditional_profile']['score_version']
    for v in variants:
        assert v.metadata['strategy_mode']=='shadow' and v.metadata['shadow_only']
        assert v.metadata['strategy'] not in cfg.execution_strategies(TradingMode.PAPER_TRADING)
        assert v.metadata['strategy'] not in cfg.execution_strategies(TradingMode.MAINNET_LIVE)
        assert v.metadata['exit_profile_strategy']==source
        assert v.entry_price==s.entry_price and v.stop_loss==s.stop_loss and v.take_profit==s.take_profit
    history=[{'metadata':{'measurement_shadow':a}}]
    assert _conditional_shadow_source_seen(history,a)
    assert not _conditional_shadow_source_seen(history,b)


def test_observe_admits_directional_disagreement_only_in_shadow():
    s=signal();s.metadata['order_flow'].update(alignment='against',score='0.2')
    s.metadata['p8_order_flow']=deepcopy(s.metadata['order_flow'])
    cfg=config().strategy
    variants,decisions=_p8_shadow_variants(s,cfg)
    assert len(variants)==1 and variants[0].metadata['strategy']=='P8_SQZ_COND_OBSERVE_SHADOW'
    assert decisions['control']['rejection']
    assert _order_flow_entry_rejection_reason(s,cfg)  # paper still blocks it
    assert _order_flow_entry_rejection_reason(variants[0],cfg)[0]=='SHADOW_ONLY'


def test_p8_fails_closed_without_corrected_flow_and_does_not_recurse():
    s=signal();s.metadata.pop('p8_order_flow')
    assert _p8_shadow_variants(s,config().strategy)[0]==[]
    variant=_p8_shadow_variants(signal(),config().strategy)[0][0]
    assert _p8_shadow_variants(variant,config().strategy)==([], {})


def test_corrected_liquidity_is_separate_from_legacy_control():
    cfg=config().edge_filters;current=SimpleNamespace(close=Decimal('105'))
    recent=[SimpleNamespace(high=Decimal('100'),low=Decimal('90'))]
    legacy=OrderFlowAnnotator(cfg,legacy_liquidity=True)
    p8=OrderFlowAnnotator(cfg)
    assert legacy._liquidity_distances(current,recent)[0]>0
    assert p8._liquidity_distances(current,recent)[0] is None


def test_runtime_p8_method_writes_only_to_shadow():
    outputs=[]
    async def capture(*args):outputs.append(args)
    bot=TradingBot.__new__(TradingBot)
    bot.config=config()
    bot._record_ml_feature_snapshot=capture
    bot._record_shadow_signal=capture
    asyncio.run(bot._record_p8_shadow(signal()))
    assert len(outputs)==3  # one decision record, two virtual writes; no order manager required
    assert all(x[0].metadata['shadow_only'] for x in outputs[1:])


def test_api_keeps_new_p8_arms_in_separate_cohorts():
    from bot_control_v2 import build_conditional_edge_report, _is_conditional_shadow_lab_strategy
    variants,_=_p8_shadow_variants(signal(),config().strategy)
    rows=[dict(id=i,created_at='2026-09-14 12:00:00',status='CLOSED',
               strategy=v.metadata['strategy'],realized_pnl='1',r_multiple='.5',
               metadata={'signal_metadata':v.metadata}) for i,v in enumerate(variants,1)]
    assert all(_is_conditional_shadow_lab_strategy(r['strategy']) for r in rows)
    report=build_conditional_edge_report(rows)
    assert report['totals']['entries']==2
    assert len({s['cohort'] for s in report['summaries']})==2
